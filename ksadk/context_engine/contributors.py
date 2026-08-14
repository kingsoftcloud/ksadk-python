"""ContextContributor —— 受控动态 Context 扩展点（方案 §8.7 / §17.2）。

动态上下文（Git、规则文件、Memory Recall、Skill manifest、附件、控制面 policy）必须通过
Contributor 统一进入，不能由 Runner/Hook/业务代码直接拼接到 Prompt（ADR-012）。Contributor
只负责产生候选 ``ContextItem``，Planner 拥有是否进入请求的最终决策；外部内容不能借此提升
权限（trust_level 不高于注册配置）。

首批内置 Contributor（方案 §8.7）：WorkspaceRules / Git / MemoryRecall / SkillManifest /
Attachment / ControlPlanePolicy。P0/P2 接入顺序见方案 §5.5/§5.8：先 shadow 收集来源与状态，
P2 再逐个接管真实来源。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from ksadk.context_engine.models import ContextItem, ContextTrustLevel

logger = logging.getLogger(__name__)

ContributorFailureMode = Literal["skip", "warn", "fail"]
ContributorCacheability = Literal["stable", "turn", "none"]


@dataclass(frozen=True)
class ContributorCapabilities:
    """Contributor 的能力与约束声明（方案 §8.7）。

    ``trust_level`` 不得高于注册配置（方案 §19）；外部 Hook/MCP 一律 ``untrusted``，不能生成
    ``platform_safety``、不能改变 ownership、不能绕过 approval。
    """

    contributor_id: str
    trust_level: ContextTrustLevel
    max_tokens: int
    timeout_ms: int
    cacheability: ContributorCacheability
    failure_mode: ContributorFailureMode


@dataclass(frozen=True)
class ContextContributionRequest:
    """单次 Contributor 贡献请求。"""

    user_input: str
    session_id: str
    invocation_id: str
    workspace_root: str = ""
    user_id: str = ""
    agent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextContributor:
    """Contributor 基类。子类实现 ``contribute`` 产生候选 ContextItem。

    返回的 ContextItem 一律按 ``capabilities.trust_level`` 标记信任级别；Planner 仍拥有是否
    进入请求的最终决策。Contributor 不得自行决定高优先级或 required。
    """

    capabilities: ContributorCapabilities

    async def contribute(self, request: ContextContributionRequest) -> list[ContextItem]:  # noqa: B027
        return []

    def id(self) -> str:
        return self.capabilities.contributor_id


def _make_item(
    *,
    contributor_id: str,
    trust_level: ContextTrustLevel,
    kind: str,
    content: Any,
    tokens: int,
    source: str,
    score: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> ContextItem:
    return ContextItem(
        item_id=f"contrib:{contributor_id}:{kind}",
        kind=kind,  # type: ignore[arg-type]
        content=content,
        source=source,
        trust_level=trust_level,
        priority=0,
        estimated_tokens=tokens,
        required=False,  # Contributor 不得自行声明 required（方案 §8.7）
        droppable=True,
        truncatable=True,
        score=score,
        metadata={"contributor_id": contributor_id, **(metadata or {})},
    )


# ---- 首批内置 Contributor（接口 + 默认实现，shadow 优先）----


class WorkspaceRulesContributor(ContextContributor):
    """工作区规则文件（AGENTS.md / CLAUDE.md） Contributor（方案 §7.6 / §8.7）。

    复用 ``ksadk.prompts.sources.discover_instruction_files`` 的确定性发现逻辑；``trust_level``
    固定 ``developer``，不高于平台安全。默认 ``cacheability=turn``（规则文件按 turn 读一次）。
    """

    def __init__(
        self,
        *,
        max_tokens: int = 12000,
        timeout_ms: int = 3000,
        failure_mode: ContributorFailureMode = "skip",
    ) -> None:
        self.capabilities = ContributorCapabilities(
            contributor_id="workspace_rules",
            trust_level="developer",
            max_tokens=max_tokens,
            timeout_ms=timeout_ms,
            cacheability="turn",
            failure_mode=failure_mode,
        )

    async def contribute(self, request: ContextContributionRequest) -> list[ContextItem]:
        from ksadk.prompts.sources import discover_instruction_files

        sections = discover_instruction_files(request.workspace_root or None)
        if not sections:
            return []
        items: list[ContextItem] = []
        for index, section in enumerate(sections):
            items.append(
                _make_item(
                    contributor_id=self.capabilities.contributor_id,
                    trust_level=self.capabilities.trust_level,
                    kind="resource_manifest",
                    content=section.content,
                    tokens=int(section.metadata.get("tokens", 0)) or 1,
                    source=section.source,
                    metadata={"path": section.metadata.get("path"), "kind": "rule_file"},
                )
            )
        return items


class MemoryRecallContributor(ContextContributor):
    """Memory Recall Contributor（方案 §8.7 / §10.6）。

    调用 ``MemoryCoordinator.recall`` 召回长期记忆，失败返回空（不污染模型输入，方案 §10.8）。
    返回的 ContextItem 一律 ``untrusted``，不能覆盖 PromptSection（方案 §8.1 / §19）。
    """

    def __init__(
        self,
        coordinator: Any,
        *,
        max_tokens: int = 4000,
        timeout_ms: int = 3000,
        top_k: int = 8,
        min_score: float = 0.45,
    ) -> None:
        self._coordinator = coordinator
        self._top_k = top_k
        self._min_score = min_score
        self.capabilities = ContributorCapabilities(
            contributor_id="memory_recall",
            trust_level="untrusted",
            max_tokens=max_tokens,
            timeout_ms=timeout_ms,
            cacheability="turn",
            failure_mode="skip",
        )

    async def contribute(self, request: ContextContributionRequest) -> list[ContextItem]:
        from ksadk.memory.coordinator import (
            agent_user_scope_id,
            build_search_request,
            recall_to_context_item,
        )

        req = build_search_request(
            query=request.user_input,
            user_id=agent_user_scope_id(
                agent_id=request.agent_id,
                user_id=request.user_id,
            ),
            top_k=self._top_k,
            max_tokens=self.capabilities.max_tokens,
            min_score=self._min_score,
        )
        result = self._coordinator.recall(req)
        ctx = recall_to_context_item(result)
        if ctx is None:
            return []
        from ksadk.context_engine.tokenizer import get_default_token_counter

        tokens = get_default_token_counter().count_text(ctx["formatted_text"])
        return [
            _make_item(
                contributor_id=self.capabilities.contributor_id,
                trust_level=self.capabilities.trust_level,
                kind="recalled_memory",
                content=ctx["formatted_text"],
                tokens=tokens,
                source="memory_provider",
                score=None,
                metadata={"recall_count": ctx.get("recall_count", 0), "status": result.status},
            )
        ]


class SkillManifestContributor(ContextContributor):
    """Skill manifest Contributor（方案 §7.7 / §8.7）：只暴露 name/desc/version，不进正文。"""

    def __init__(
        self,
        manifests: Sequence[dict[str, Any]] | None = None,
        *,
        max_tokens: int = 8000,
        timeout_ms: int = 3000,
    ) -> None:
        self._manifests = list(manifests or [])
        self.capabilities = ContributorCapabilities(
            contributor_id="skill_manifest",
            trust_level="resource",
            max_tokens=max_tokens,
            timeout_ms=timeout_ms,
            cacheability="stable",
            failure_mode="skip",
        )

    def set_manifests(self, manifests: Sequence[dict[str, Any]]) -> None:
        self._manifests = list(manifests)

    async def contribute(self, request: ContextContributionRequest) -> list[ContextItem]:
        if not self._manifests:
            return []
        import json

        text = json.dumps(self._manifests, ensure_ascii=False)
        from ksadk.context_engine.tokenizer import get_default_token_counter

        return [
            _make_item(
                contributor_id=self.capabilities.contributor_id,
                trust_level=self.capabilities.trust_level,
                kind="resource_manifest",
                content=text,
                tokens=get_default_token_counter().count_text(text),
                source="skill_manifest",
                metadata={"skill_count": len(self._manifests)},
            )
        ]


# ---- 并发执行与约束（方案 §8.7 / §17.2）----


@dataclass(frozen=True)
class ContributionResult:
    """一批 Contributor 的执行结果。"""

    items: list[ContextItem]
    status: dict[str, str]  # contributor_id → "ok" / "timeout" / "error" / "skipped"
    warnings: tuple[str, ...]


async def run_contributors(
    contributors: Sequence[ContextContributor],
    request: ContextContributionRequest,
    *,
    default_timeout_ms: int = 3000,
    default_failure_mode: ContributorFailureMode = "skip",
) -> ContributionResult:
    """并发执行 Contributors，各自受超时、预算与 failure policy 约束（方案 §8.7）。

    - 超时 → 该 Contributor 返回空，status=timeout。
    - 异常 → 按 failure_mode：skip 返空 / warn 返空 + warning / fail 抛给上层。
    - 返回的 ContextItem 总 token 受各自 ``max_tokens`` 约束（Planner 再做全局预算）。
    """

    async def _run_one(c: ContextContributor) -> tuple[str, list[ContextItem], str, str | None]:
        timeout = max(c.capabilities.timeout_ms, 1) / 1000.0
        try:
            items = await asyncio.wait_for(c.contribute(request), timeout=timeout)
        except asyncio.TimeoutError:
            return c.id(), [], "timeout", f"{c.id()}: timeout"
        except Exception as exc:  # noqa: BLE001
            if c.capabilities.failure_mode == "fail":
                raise
            return c.id(), [], "error", f"{c.id()}: {exc}"
        # 单 Contributor 总 token 约束
        if items and sum(i.estimated_tokens for i in items) > c.capabilities.max_tokens:
            items = items[: c.capabilities.max_tokens]  # best-effort 限条数
        return c.id(), items, "ok", None

    results = await asyncio.gather(*[_run_one(c) for c in contributors], return_exceptions=True)
    all_items: list[ContextItem] = []
    status: dict[str, str] = {}
    warnings: list[str] = []
    for contributor, r in zip(contributors, results):
        if isinstance(r, Exception):
            if contributor.capabilities.failure_mode == "fail":
                raise r
            warnings.append(f"{contributor.id()}: {r}")
            status[contributor.id()] = "error"
            continue
        cid, items, st, warn = r
        status[cid] = st
        all_items.extend(items)
        if warn:
            warnings.append(warn)
    return ContributionResult(items=all_items, status=status, warnings=tuple(warnings))


def run_contributors_sync(
    contributors: Sequence[ContextContributor],
    request: ContextContributionRequest,
    **kwargs: Any,
) -> ContributionResult:
    """同步入口（无运行中事件循环时用 ``asyncio.run``）。"""
    try:
        asyncio.get_running_loop()
        raise RuntimeError("call run_contributors within a running loop instead")
    except RuntimeError as exc:
        if "call run_contributors" in str(exc):
            raise
        # 无运行中 loop → asyncio.run 安全
        return asyncio.run(run_contributors(contributors, request, **kwargs))


__all__ = [
    "ContributionResult",
    "ContextContributionRequest",
    "ContextContributor",
    "ContributorCapabilities",
    "MemoryRecallContributor",
    "SkillManifestContributor",
    "WorkspaceRulesContributor",
    "run_contributors",
    "run_contributors_sync",
]
