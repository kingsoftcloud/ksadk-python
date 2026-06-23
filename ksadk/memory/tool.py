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


def _is_sdk_backend(service: LongTermMemoryService) -> bool:
    backend = getattr(service, "_backend", None)
    return backend.__class__.__name__ == "SdkLTMBackend"


def _sdk_pending_result(service: LongTermMemoryService, context) -> dict:
    backend = getattr(service, "_backend", None)
    session_status = None
    get_session_status = getattr(backend, "get_session_status", None)
    if callable(get_session_status):
        session_status = get_session_status(
            user_id=context.user_id,
            session_id=context.session_id,
        )

    result = {
        "ok": False,
        "status": "accepted_not_extracted",
        "message": "记忆保存请求已被后端受理，但尚未抽取成可检索记忆。",
    }
    if isinstance(session_status, dict):
        result["session_state"] = session_status.get("State")
        result["session_id"] = session_status.get("SessionId") or context.session_id
    elif context.session_id:
        result["session_id"] = context.session_id
    return result


def save_memory(content: str) -> dict:
    context = get_current_invocation_context()
    if context is None:
        return {
            "ok": False,
            "status": "failed",
            "message": "记忆保存失败: 缺少运行时上下文。",
        }

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
            if _is_sdk_backend(service):
                entries = service.search_entries(user_id=context.user_id, query=content, top_k=1)
                if not entries:
                    return _sdk_pending_result(service, context)
            return {"ok": True, "status": "persisted", "message": "记忆已保存。"}

        backend = getattr(service, "_backend", None)
        last_error = str(getattr(backend, "last_error", "") or "").strip()
        message = f"记忆保存失败: {last_error}" if last_error else "记忆保存失败。"
        return {"ok": False, "status": "failed", "message": message}
    except Exception as exc:
        logger.error("save_memory failed: %s", exc)
        return {"ok": False, "status": "failed", "message": f"记忆保存失败: {exc}"}
