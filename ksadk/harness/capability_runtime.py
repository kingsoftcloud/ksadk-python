"""统一 CapabilityRuntime（plan §10/§11 收口 2）。

把 ToolPolicy、ToolReceipt、Approval、Sandbox 装配到同一个执行节点入口：
tool_calls 节点对每次调用统一走「幂等检查 → 策略决策 → 审批 → 执行 → Receipt
落账」，MCP/Skill 工具经 ToolExecutor 注入（宿主负责装配数据面），Sandbox
Backend 作为能力面由宿主声明并可在 ToolExecutor 内使用。

职责边界：
- 本模块只做决策与幂等（纯逻辑 + 可选 SQLite Receipt 存储），不做 IO 副作用；
- 「如何挂起等审批」仍由引擎的 ApprovalResolver 承担（LangGraph interrupt）；
- Receipt 以 (invocation_id, call_id) 为幂等键：审批恢复重放节点时直接回放
  既有结果，不重复触发外部副作用（plan §11.2）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ksadk.harness.capabilities import RiskLevel
from ksadk.harness.tool_policy import ToolCallContext, ToolDecision, ToolPolicy
from ksadk.harness.tool_receipts import ToolReceipt, ToolReceiptStore
from ksadk.harness.tool_reliability import ToolReliability, classify_tool_reliability

# G23 ① bypass 免疫默认清单（ApprovalPolicy.immune_prefixes/paths 可覆盖）
_DEFAULT_IMMUNE_PREFIXES: tuple[str, ...] = (
    "delete", "drop", "rm", "remove", "truncate", "purge", "wipe",
    "sudo", "chmod", "chown", "shutdown", "reboot",
)
_DEFAULT_IMMUNE_PATHS: tuple[str, ...] = (
    "~/.ssh", "~/.aws", "~/.config/gcloud", "~/.kube",
    "/etc/", "/root/", "~/.bashrc", "~/.zshrc", "~/.profile",
    ".env", "id_rsa", "credentials", "secrets",
)

#: side_effect（Studio ToolContract）→ 是否存在外部副作用。
_SIDE_EFFECT_EXTERNAL = frozenset({"write", "external"})


def arguments_digest(arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _risk_from_side_effect(side_effect: str) -> RiskLevel:
    if side_effect == "external":
        return RiskLevel.HIGH
    if side_effect == "write":
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


@dataclass
class ToolProfile:
    """宿主注册的工具画像（来自 ToolContract / MCP 声明）。"""

    name: str
    version: str = "1.0.0"
    risk_level: RiskLevel = RiskLevel.LOW
    side_effect: str = "none"
    data_sensitivity: str = "internal"
    network_destination: str = ""


class CapabilityRuntime:
    """tool_calls 节点的统一决策/幂等入口。

    - ``decide``：ToolPolicy 确定性决策（allow / deny / require_approval）；
    - ``check_receipt`` / ``record_receipt``：审批恢复重放的幂等锚点；
    - 宿主未注入 Policy/Receipt 时自动退化为「全部放行」（调试语义）。
    """

    def __init__(
        self,
        *,
        policy: ToolPolicy | None = None,
        receipts: ToolReceiptStore | None = None,
        profiles: dict[str, ToolProfile] | None = None,
        environment: str = "draft",
        approval_mode: str = "policy",
        classifier_available: bool = True,
        immune_prefixes: tuple[str, ...] = (),
        immune_paths: tuple[str, ...] = (),
    ) -> None:
        self._policy = policy or ToolPolicy()
        self._receipts = receipts
        self._profiles = dict(profiles or {})
        self._environment = environment
        self._approval_mode = approval_mode
        # G23 审批治理面
        self._classifier_available = classifier_available
        self._immune_prefixes = immune_prefixes or _DEFAULT_IMMUNE_PREFIXES
        self._immune_paths = immune_paths or _DEFAULT_IMMUNE_PATHS
        # 熔断状态（per-runtime 内存，不跨进程；G32 范围才做跨进程）
        self._consecutive_denials: dict[str, int] = {}
        self._total_denials: dict[str, int] = {}

    # -------------------------------------------------------------- policy

    def register_profile(self, profile: ToolProfile) -> None:
        self._profiles[profile.name] = profile

    @property
    def receipt_enabled(self) -> bool:
        """当前执行入口是否装配 Receipt Store。"""
        return self._receipts is not None

    def reliability(
        self,
        tool_name: str,
        *,
        side_effect: str | None = None,
        transport_idempotent: bool = False,
    ) -> ToolReliability:
        """返回 Tool 的诚实可靠性声明。

        ``side_effect`` 允许 MCP/子 Agent 等动态工具按实际目标覆盖静态画像；
        未注册工具保持 ``unknown``，不会被误标为无副作用。
        """
        profile = self._profiles.get(tool_name)
        resolved_side_effect = side_effect or (profile.side_effect if profile else "unknown")
        return classify_tool_reliability(
            side_effect=resolved_side_effect,
            receipt_enabled=self.receipt_enabled,
            transport_idempotent=transport_idempotent,
        )

    def decide(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> ToolDecision:
        profile = self._profiles.get(tool_name)
        risk = profile.risk_level if profile else RiskLevel.LOW
        policy_ref = self._policy.policy_ref
        prior = self.check_receipt_idempotency_only(tenant_id, tool_name)
        policy_decision = self._policy.decide(
            ToolCallContext(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                tool_name=tool_name,
                tool_version=profile.version if profile else "1.0.0",
                risk_level=risk,
                has_external_side_effects=(
                    profile.side_effect in _SIDE_EFFECT_EXTERNAL if profile else False
                ),
                data_sensitivity=profile.data_sensitivity if profile else "internal",
                network_destination=profile.network_destination if profile else "",
                environment=self._environment,
                prior_receipt_id=prior or "",
            )
        )
        # Approval can authorize an ask, never override a revision's hard deny.
        # This also applies to never mode and an unavailable classifier.
        if policy_decision.action == "deny":
            return policy_decision
        # G23 ③ 熔断：连续 3 拒/累计 20 拒 → deny
        breaker_key = f"{tenant_id}:{user_id}:{agent_id}"
        if (
            self._consecutive_denials.get(breaker_key, 0) >= 3
            or self._total_denials.get(breaker_key, 0) >= 20
        ):
            return ToolDecision(
                action="deny",
                reason="approval circuit breaker open (3 consecutive or 20 total denials)",
                policy_ref=policy_ref,
                risk_level=risk,
                disclosure="熔断器打开，拒绝调用",
                decided_by="circuit_breaker",
            )
        # G23 ① bypass 免疫：敏感路径/敏感动词不得被 never 模式覆盖
        immune_reason = self._check_immune(tool_name, arguments)
        if immune_reason:
            return ToolDecision(
                action="require_approval",
                reason=immune_reason,
                policy_ref=policy_ref,
                risk_level=risk,
                disclosure="bypass 免疫：敏感面不可被 never 模式覆盖",
                decided_by="immune",
            )
        # G23 ③ fail-closed：分类器不可用 → require_approval（不 allow）
        if not self._classifier_available:
            return ToolDecision(
                action="require_approval",
                reason="classifier unavailable (fail-closed)",
                policy_ref=policy_ref,
                risk_level=risk,
                disclosure="分类器不可用，fail-closed",
                decided_by="config",
            )
        if self._approval_mode == "never":
            return ToolDecision(
                action="allow",
                reason="approval_policy.mode=never",
                policy_ref=policy_ref,
                risk_level=risk,
                decided_by="config",
            )
        return policy_decision

    def _check_immune(self, tool_name: str, arguments: dict[str, Any] | None) -> str:
        """G23 ① bypass 免疫检查：敏感动词/敏感路径/敏感命令。"""
        name_lower = tool_name.lower()
        for prefix in self._immune_prefixes:
            if name_lower.startswith(prefix):
                return f"immune: tool name matches sensitive prefix {prefix!r}"
        if arguments:
            for key in ("path", "file_path", "command", "script"):
                val = str(arguments.get(key) or "")
                for immune_path in self._immune_paths:
                    if immune_path in val:
                        return f"immune: argument {key} matches sensitive path {immune_path!r}"
                # command/script 参数内的敏感命令动词（rm -rf / 、sudo ... 等）
                if key in ("command", "script"):
                    val_lower = val.lower()
                    for verb in self._immune_prefixes:
                        if verb in val_lower:
                            return f"immune: {key} contains sensitive command {verb!r}"
        return ""

    def record_denial(self, tenant_id: str, user_id: str, agent_id: str) -> None:
        """G23 ③ 记录一次拒绝（供熔断计数）。外部审批 denied 后调用。"""
        key = f"{tenant_id}:{user_id}:{agent_id}"
        self._consecutive_denials[key] = self._consecutive_denials.get(key, 0) + 1
        self._total_denials[key] = self._total_denials.get(key, 0) + 1

    def reset_denials(self, tenant_id: str, user_id: str, agent_id: str) -> None:
        """审批通过后重置连续拒绝计数（总计数保留，用于累计熔断）。"""
        key = f"{tenant_id}:{user_id}:{agent_id}"
        self._consecutive_denials.pop(key, None)

    # ------------------------------------------------------------- receipts

    def check_receipt(self, invocation_id: str, call_id: str) -> ToolReceipt | None:
        if self._receipts is None:
            return None
        return self._receipts.get(invocation_id, call_id)

    def check_receipt_idempotency_only(self, tenant_id: str, tool_name: str) -> str:
        """策略决策用的历史 Receipt 存在性（键退化：tenant+tool）。"""
        del tenant_id, tool_name
        return ""

    def record_receipt(
        self,
        *,
        invocation_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        decision: str,
        status: str,
        result_digest: str = "",
    ) -> ToolReceipt | None:
        if self._receipts is None:
            return None
        return self._receipts.record(
            ToolReceipt(
                invocation_id=invocation_id,
                call_id=call_id,
                tool_name=tool_name,
                arguments_digest=arguments_digest(arguments or {}),
                decision=decision,
                status=status,
                result_digest=result_digest,
            )
        )

    # ------------------------------------------------------------ factories

    @staticmethod
    def from_tool_contracts(
        contracts: list[dict[str, Any]] | None,
        *,
        approval_mode: str = "policy",
        policy: ToolPolicy | None = None,
        receipts: ToolReceiptStore | None = None,
        environment: str = "draft",
    ) -> CapabilityRuntime:
        """从 Studio ToolContract 列表装配（risk 来自 side_effect/approval 声明）。

        approval_mode 为 never 时不强制任何审批；always 时全部进审批
        （经 approval_required_prefixes 全前缀匹配实现）。
        """
        profiles: dict[str, ToolProfile] = {}
        approval_prefixes: list[str] = []
        for contract in contracts or []:
            if not isinstance(contract, dict) or not contract.get("name"):
                continue
            name = str(contract["name"])
            side_effect = str(contract.get("sideEffect") or contract.get("side_effect") or "none")
            approval = str(contract.get("approval") or "never")
            risk = _risk_from_side_effect(side_effect)
            if approval == "always":
                risk = RiskLevel.HIGH
            profiles[name] = ToolProfile(
                name=name,
                version=str(contract.get("version") or "1.0.0"),
                risk_level=risk,
                side_effect=side_effect,
            )
            if approval == "always" or (approval == "policy" and risk is RiskLevel.HIGH):
                approval_prefixes.append(name)
        resolved_policy = policy or ToolPolicy(
            approval_required_prefixes=tuple(approval_prefixes),
        )
        if approval_mode == "always":
            resolved_policy = ToolPolicy(approval_required_prefixes=("",))
        elif approval_mode == "never":
            resolved_policy = ToolPolicy()
        return CapabilityRuntime(
            policy=resolved_policy,
            receipts=receipts,
            profiles=profiles,
            environment=environment,
            approval_mode=approval_mode,
        )


__all__ = [
    "CapabilityRuntime",
    "ToolProfile",
    "arguments_digest",
]
