"""HostedTurnFinalizer —— 统一 Studio 与 canonical Runtime 的 turn 收尾（方案 §11.1 / P0）。

之前 Studio `StudioRunService` 和 canonical `conversation_execution._finalize_hosted_turn`
各自维护一套收尾逻辑（usage 回填、Context evidence、Memory Candidate、Trace），导致两条路径
漂移（如 scope_id 硬编码 local-user、Memory 重复写）。本组件统一负责：

- actual usage 回填进 ContextPlan（planned vs actual 偏差可观测）。
- capability mismatch 证据检测（方案 §6.1）。
- Memory Candidate 抽取 + flush（据 MemoryPolicy，同一 Turn 不重复写）。
- 失败降级（best-effort，绝不阻断主链路，方案 §10.8）。

Studio 与 canonical Runtime 都调用本组件，不再各自实现收尾。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping


def _memory_extract_enabled() -> bool:
    return str(os.environ.get("KSADK_MEMORY_FLUSH_ENABLED", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _extract_input_tokens(usage: Mapping[str, Any] | None) -> int | None:
    """从 runtime usage 取 input tokens（兼容 OpenAI/Anthropic 字段）。"""
    if not isinstance(usage, Mapping):
        return None
    for key in ("input_tokens", "prompt_tokens", "input_token_count"):
        v = usage.get(key)
        if isinstance(v, (int, float)) and v:
            return int(v)
    details = usage.get("input_token_details") or usage.get("input_tokens_details")
    if isinstance(details, Mapping):
        total = details.get("total") or details.get("input_tokens")
        if isinstance(total, (int, float)) and total:
            return int(total)
    return None


@dataclass(frozen=True)
class FinalizeContext:
    """turn 收尾所需的上下文（Studio 与 canonical 共用）。"""

    session_id: str
    invocation_id: str
    user_id: str
    context_plan: dict[str, Any] | None
    shadow_context_plan: dict[str, Any] | None
    usage: Mapping[str, Any] | None
    runtime_type: str
    prompt_integration_mode: str = ""
    session_events: Any = None  # 已取的 turn events（避免重复读 store）
    memory_write_rollout: str = ""


async def finalize_hosted_turn(
    ctx: FinalizeContext,
    *,
    session_service_provider: Callable[[], Any] | None = None,
) -> None:
    """统一 hosted turn 收尾（方案 §11.1 步骤 15-16 / P0 收敛）。

    Studio 与 canonical Runtime 都调用本函数，不再各自实现。失败不阻断主链路。
    """
    # 1. usage 回填进 ContextPlan
    plan = ctx.context_plan
    if isinstance(plan, dict):
        try:
            actual = _extract_input_tokens(ctx.usage)
            if actual is not None:
                plan["runtime_reported_input_tokens"] = actual
        except Exception:  # noqa: BLE001
            pass

    # 2. capability mismatch 证据检测
    try:
        _maybe_detect_capability_mismatch(ctx)
    except Exception:  # noqa: BLE001
        pass

    # 3. Memory Candidate 抽取 + flush（据 MemoryPolicy 门控）
    # memory_write_rollout=enabled → 即使 env 没设也 flush（AgentVersion 级策略）
    # memory_write_rollout=shadow/off → 不 flush（仅观测/关闭）
    # memory_write_rollout 未设（空）→ fallback 到 env KSADK_MEMORY_FLUSH_ENABLED
    should_flush = ctx.memory_write_rollout == "enabled" or (
        not ctx.memory_write_rollout and _memory_extract_enabled()
    )
    if not should_flush:
        return
    # 不强制 prompt_integration_mode=ksadk_hosted：canonical 路径可能在非 hosted 也需 flush
    # （如 framework_assisted 的显式"记住"），只要 Memory 开关开启即 flush（方案 §10.4）。
    try:
        from ksadk.memory.coordinator import MemoryCoordinator
        from ksadk.memory.extraction import propose_memory_candidates
        from ksadk.memory.providers.local_sqlite import resolve_default_memory_provider

        # 优先用已取的 turn events；否则从 session store 读
        turn_events = ctx.session_events
        if turn_events is None and session_service_provider is not None:
            try:
                service = session_service_provider()
                all_events = await service.get_events(ctx.session_id)
                turn_events = [
                    e for e in all_events if getattr(e, "invocation_id", "") == ctx.invocation_id
                ]
            except Exception:  # noqa: BLE001
                turn_events = None
        if not turn_events:
            return
        # scope_id 由可信 user_id 决定（不硬编码 local-user，方案 §19）
        candidates = propose_memory_candidates(
            list(turn_events),
            scope="user",
            scope_id=str(ctx.user_id or ""),
        )
        if candidates:
            coordinator = MemoryCoordinator(resolve_default_memory_provider())
            coordinator.flush_candidates(candidates)
    except Exception:  # noqa: BLE001 — memory flush 绝不阻断主链路
        pass


def _maybe_detect_capability_mismatch(ctx: FinalizeContext) -> None:
    """证据驱动的 capability mismatch 熔断（方案 §6.1）。"""
    from ksadk.context_engine.capabilities import (
        capabilities_for_runtime_type,
        detect_capability_mismatch,
        is_capability_circuit_open,
        mark_capability_mismatch,
    )

    shadow = ctx.shadow_context_plan or {}
    runtime_type = str(shadow.get("runtime_type") or "")
    if not runtime_type:
        return
    if is_capability_circuit_open(runtime_type=runtime_type):
        return
    caps = capabilities_for_runtime_type(runtime_type)
    has_usage = isinstance(ctx.usage, Mapping) and bool(ctx.usage)
    reason = detect_capability_mismatch(
        declared=caps,
        runtime_reported_usage=has_usage if has_usage else False,
    )
    if reason and "runtime_reported" in reason:
        mark_capability_mismatch(runtime_type=runtime_type)


__all__ = ["FinalizeContext", "finalize_hosted_turn"]
