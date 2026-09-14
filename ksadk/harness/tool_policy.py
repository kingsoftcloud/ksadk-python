"""Harness Tool Policy 决策（plan §11.1）。

确定性决策表（无模型调用）：

- CRITICAL 风险或外部副作用 + 高敏数据 → require_approval；
- 策略显式 deny（如目的地不在允许清单）→ deny；
- 其余 → allow。

决策输入覆盖 plan 要求的最小集：tenant/user/agent、tool+版本、参数摘要、
敏感等级、外部副作用、网络目的地、环境、Revision Policy、历史 Receipt。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ksadk.harness.capabilities import RiskLevel

#: 风险等级序（str-Enum 不可比较，用显式序号）。
_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}
#: 审批阈值（含）：该风险等级及以上需要审批。
_APPROVAL_RISK = 2  # HIGH
#: 拒绝阈值（含）：该风险等级及以上在正式环境需审批。
_DENY_RISK = 3  # CRITICAL


@dataclass(frozen=True)
class ToolCallContext:
    """一次 Tool 调用的决策输入。"""

    tenant_id: str
    user_id: str
    agent_id: str
    tool_name: str
    tool_version: str = "1.0.0"
    risk_level: RiskLevel = RiskLevel.LOW
    has_external_side_effects: bool = False
    data_sensitivity: str = "internal"  # public/internal/confidential/restricted
    network_destination: str = ""
    environment: str = "draft"  # draft / revision
    #: Revision Policy 侧白名单：为空表示不限制目的地。
    allowed_destinations: tuple[str, ...] = ()
    #: 历史 Receipt：此前是否已成功执行同一 call（幂等去重）。
    prior_receipt_id: str = ""


@dataclass(frozen=True)
class ToolDecision:
    action: str  # allow / deny / require_approval
    reason: str
    policy_ref: str
    risk_level: RiskLevel
    disclosure: str = ""


@dataclass
class ToolPolicy:
    """Revision 级工具策略（宿主从 HarnessSpec.tool_policy 投影）。"""

    policy_ref: str = "tool-policy://default@1"
    #: 强制审批的工具名前缀（如高危财务工具）。
    approval_required_prefixes: tuple[str, ...] = field(default=())
    #: 明确拒绝的工具名前缀。
    denied_prefixes: tuple[str, ...] = field(default=())
    #: 允许的网络目的地（空 = 不限制）。
    allowed_destinations: tuple[str, ...] = field(default=())
    #: 环境限制：正式环境默认更严。
    strict_in_revision: bool = True

    def decide(self, context: ToolCallContext) -> ToolDecision:
        if any(context.tool_name.startswith(p) for p in self.denied_prefixes):
            return ToolDecision(
                action="deny",
                reason=f"tool {context.tool_name} is denied by revision policy",
                policy_ref=self.policy_ref,
                risk_level=context.risk_level,
                disclosure="Revision Policy 明确拒绝该工具",
            )
        if self.allowed_destinations and context.network_destination:
            if context.network_destination not in self.allowed_destinations:
                return ToolDecision(
                    action="deny",
                    reason=(
                        f"network destination {context.network_destination!r} "
                        f"not in allowlist {list(self.allowed_destinations)}"
                    ),
                    policy_ref=self.policy_ref,
                    risk_level=context.risk_level,
                    disclosure="网络目的地不在允许清单",
                )
        if _RISK_ORDER[context.risk_level] >= _DENY_RISK and context.environment == "revision":
            if not context.prior_receipt_id:
                return ToolDecision(
                    action="require_approval",
                    reason="critical risk in revision environment requires approval",
                    policy_ref=self.policy_ref,
                    risk_level=context.risk_level,
                    disclosure="Critical 风险工具在正式环境需审批",
                )
        needs_approval = (
            _RISK_ORDER[context.risk_level] >= _APPROVAL_RISK
            or context.has_external_side_effects
            or context.data_sensitivity in {"confidential", "restricted"}
            or any(context.tool_name.startswith(p) for p in self.approval_required_prefixes)
        )
        if needs_approval and not context.prior_receipt_id:
            return ToolDecision(
                action="require_approval",
                reason=(
                    f"risk={context.risk_level.value}, "
                    f"side_effects={context.has_external_side_effects}, "
                    f"sensitivity={context.data_sensitivity}"
                ),
                policy_ref=self.policy_ref,
                risk_level=context.risk_level,
                disclosure="高风险/外部副作用/敏感数据工具需审批",
            )
        return ToolDecision(
            action="allow",
            reason="within policy",
            policy_ref=self.policy_ref,
            risk_level=context.risk_level,
        )


__all__ = ["ToolCallContext", "ToolDecision", "ToolPolicy"]
