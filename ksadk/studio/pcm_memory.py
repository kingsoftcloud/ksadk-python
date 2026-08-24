"""Studio-side projection helpers for platform-owned PCM memory."""

from __future__ import annotations

from typing import Any

from ksadk.memory.coordinator import (
    MemoryCoordinator,
    agent_user_scope_id,
    build_search_request,
    recall_to_context_item,
)
from ksadk.memory.events import recall_completed, recall_empty, recall_failed
from ksadk.memory.provider_adapter import adapt_as_memory_provider
from ksadk.memory.provider_resolver import resolve_memory_provider


def recall_platform_memory(
    *,
    run_id: str,
    session_id: str,
    agent_id: str,
    user_id: str,
    user_input: str,
    request_config: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Recall agent-scoped memory and return its auditable lifecycle event."""

    if not bool(request_config.get("memory_enabled")) or not bool(
        request_config.get("memory_recall_enabled", True)
    ):
        return None, []

    provider_ref = str(request_config.get("provider_ref") or "local-default")
    rollout = str(request_config.get("memory_write_rollout") or "enabled")
    try:
        provider = adapt_as_memory_provider(resolve_memory_provider(provider_ref))
        result = MemoryCoordinator(provider).recall(
            build_search_request(
                query=user_input,
                user_id=agent_user_scope_id(agent_id=agent_id, user_id=user_id),
                top_k=int(request_config.get("memory_recall_top_k") or 8),
                max_tokens=int(request_config.get("memory_recall_max_tokens") or 1600),
                min_score=float(request_config.get("memory_recall_min_score") or 0.45),
            )
        )
        context = recall_to_context_item(result)
        if context is not None:
            event = recall_completed(
                run_id=run_id,
                session_id=session_id,
                provider=provider_ref,
                rollout=rollout,
                count=len(result.records),
            )
        elif result.status == "ok":
            event = recall_empty(
                run_id=run_id,
                session_id=session_id,
                provider=provider_ref,
                rollout=rollout,
            )
        else:
            event = recall_failed(
                run_id=run_id,
                session_id=session_id,
                provider=provider_ref,
                rollout=rollout,
                error_code=str(result.error_code or result.status),
                error_message="平台长期记忆召回失败",
                retryable=result.status in {"timeout", "failed"},
            )
        return context, [event.to_dict()]
    except Exception as exc:  # noqa: BLE001 - recall failure must not break a run
        return None, [
            recall_failed(
                run_id=run_id,
                session_id=session_id,
                provider=provider_ref,
                rollout=rollout,
                error_code="recall_exception",
                error_message=str(exc)[:200],
                retryable=True,
            ).to_dict()
        ]
