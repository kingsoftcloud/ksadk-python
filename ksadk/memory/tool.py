"""Cross-framework long-term memory tools."""

from __future__ import annotations

import logging

from ksadk.memory.service import LongTermMemoryService
from ksadk.runtime_context import get_current_invocation_context

logger = logging.getLogger(__name__)

_service: LongTermMemoryService | None = None


def _get_or_create_service() -> LongTermMemoryService:
    global _service
    if _service is None:
        _service = LongTermMemoryService.from_env()
    return _service


def load_memory(query: str) -> str:
    context = get_current_invocation_context()
    if context is None:
        return "长期记忆检索失败: 缺少运行时上下文。"

    try:
        service = _get_or_create_service()
        return service.search_text(user_id=context.user_id, query=query)
    except Exception as exc:
        logger.error("load_memory failed: %s", exc)
        return f"长期记忆检索失败: {exc}"


def save_memory(content: str) -> str:
    context = get_current_invocation_context()
    if context is None:
        return "记忆保存失败: 缺少运行时上下文。"

    try:
        service = _get_or_create_service()
        ok = service.save_text(
            user_id=context.user_id,
            content=content,
            metadata={
                "agent_id": context.agent_id,
                "session_id": context.session_id,
                "runner_type": context.runner_type,
            },
        )
        if ok:
            return "记忆已保存。"

        backend = getattr(service, "_backend", None)
        last_error = str(getattr(backend, "last_error", "") or "").strip()
        return f"记忆保存失败: {last_error}" if last_error else "记忆保存失败。"
    except Exception as exc:
        logger.error("save_memory failed: %s", exc)
        return f"记忆保存失败: {exc}"
