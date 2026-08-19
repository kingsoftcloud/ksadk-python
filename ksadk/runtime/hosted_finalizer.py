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
    agent_id: str = ""
    prompt_integration_mode: str = ""
    session_events: Any = None  # 已取的 turn events（避免重复读 store）
    memory_write_rollout: str = ""
    memory_enabled: bool | None = None
    memory_recall_enabled: bool | None = None
    memory_write_mode: str = "candidate"
    flush_before_compaction: bool = True
    provider_ref: str = "local-default"
    emit_event: Any = None


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

    # 3. Memory Candidate 抽取 + flush
    # 统一解析 Memory 运行策略（方案 §2：ResolvedMemoryPolicy 统一入口）
    from ksadk.memory.resolved_policy import resolve_memory_policy

    policy = resolve_memory_policy(
        memory_enabled=ctx.memory_enabled,
        recall_enabled=ctx.memory_recall_enabled,
        write_rollout=ctx.memory_write_rollout,
        write_mode=ctx.memory_write_mode,
        flush_before_compaction=ctx.flush_before_compaction,
        provider_ref=ctx.provider_ref,
    )
    # shadow：生成 Candidate 和审计事件，但不提交 Provider（方案 §2）
    # off/不启用：直接返回，连候选都不提取
    if not policy.should_extract_candidates:
        return
    try:
        from ksadk.memory.coordinator import MemoryCoordinator
        from ksadk.memory.events import (
            candidate_created,
            candidate_rejected,
            flush_completed,
            flush_failed,
        )
        from ksadk.memory.extraction import propose_memory_candidates
        from ksadk.memory.provider_resolver import resolve_memory_provider

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
        from ksadk.memory.coordinator import agent_user_scope_id

        candidates = propose_memory_candidates(
            list(turn_events),
            scope="user",
            scope_id=agent_user_scope_id(
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            ),
        )
        # explicit_only：只保留用户明确要求记住的内容（方案 §2）
        if policy.is_explicit_only:
            candidates = [c for c in candidates if c.reason.startswith("explicit_user_")]
        provider_name = ctx.provider_ref or "local-default"
        rollout = policy.write_rollout
        if candidates:
            # shadow：生成候选和审计事件，但不提交 Provider（方案 §2）
            if policy.should_flush:
                provider = resolve_memory_provider(ctx.provider_ref)
                from ksadk.memory.provider_adapter import adapt_as_memory_provider

                provider = adapt_as_memory_provider(provider)
                coordinator = MemoryCoordinator(provider)
                result = coordinator.flush_candidates(candidates)
            else:
                # shadow：不提交，构造一个不落库的 result
                from ksadk.memory.coordinator import FlushResult

                result = FlushResult(
                    status="shadow",
                    proposed=len(candidates),
                    committed=0,
                    rejected=0,
                )
            _emit(
                ctx,
                candidate_created(
                    run_id=ctx.invocation_id,
                    session_id=ctx.session_id,
                    provider=provider_name,
                    rollout=rollout,
                    count=len(candidates),
                ),
            )
            if result.rejected > 0:
                _emit(
                    ctx,
                    candidate_rejected(
                        run_id=ctx.invocation_id,
                        session_id=ctx.session_id,
                        provider=provider_name,
                        rollout=rollout,
                        count=result.rejected,
                    ),
                )
            # 检查 flush 结果：partial/failed 时发 flush.failed（方案 §3）
            if result.status in ("succeeded", "shadow"):
                _emit(
                    ctx,
                    flush_completed(
                        run_id=ctx.invocation_id,
                        session_id=ctx.session_id,
                        provider=provider_name,
                        rollout=rollout,
                        proposed=result.proposed,
                        committed=result.committed,
                        rejected=result.rejected,
                    ),
                )
            else:
                # partial / failed → flush.failed
                _emit(
                    ctx,
                    flush_failed(
                        run_id=ctx.invocation_id,
                        session_id=ctx.session_id,
                        provider=provider_name,
                        rollout=rollout,
                        error_code=f"flush_{result.status}",
                        error_message=f"{result.status}: {len(result.errors)} errors",
                        retryable=True,
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        _emit(
            ctx,
            flush_failed(
                run_id=ctx.invocation_id,
                session_id=ctx.session_id,
                provider="sqlite",
                rollout=ctx.memory_write_rollout or "enabled",
                error_code="flush_exception",
                error_message=str(exc)[:200],
                retryable=True,
            ),
        )


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


# 内存事件收集器（best-effort，不阻断主链路）
def _emit(ctx: Any, event: Any) -> None:
    """发送 Memory 事件到 ctx.emit_event（方案 §3）。"""
    sink = getattr(ctx, "emit_event", None)
    if callable(sink):
        try:
            sink(event.to_dict())
        except Exception:  # noqa: BLE001
            pass
