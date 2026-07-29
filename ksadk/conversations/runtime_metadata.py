from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Dict, Mapping, Optional, Sequence

import httpx

from ksadk.conversations.attachments import compact_attachment_result_for_session
from ksadk.conversations.context import (
    canonical_event_type,
)
from ksadk.conversations.model_context import (
    normalize_model_metadata,
)
from ksadk.conversations.normalize import (
    canonical_input_content_from_parts,
    compact_attachment_for_session,
    normalize_kop_messages,
)
from ksadk.conversations.reasoning_markup import strip_reasoning_markup
from ksadk.conversations.runtime_constants import (
    _MODEL_CATALOG_CACHE,
    _MODEL_CATALOG_CACHE_TTL_SECONDS,
    ATTACHMENT_CONTEXT_STATE_KEY,
    SESSION_SUMMARY_MAX_CHARS,
    logger,
)
from ksadk.conversations.session_title import (
    DEFAULT_SESSION_TITLE_TIMEOUT_MS,
    HEURISTIC_SESSION_TITLE_SOURCE,
    build_fallback_title,
    build_heuristic_title,
    build_session_title_messages,
    is_low_quality_title,
    resolve_session_title_client,
    resolve_session_title_model,
)
from ksadk.sessions import Session, SessionEvent


def _truncate_text(text: str | None, limit: int) -> str:
    raw = " ".join(str(text or "").strip().split())
    if len(raw) <= limit:
        return raw
    return f"{raw[: max(limit - 1, 0)].rstrip()}…"


async def _update_session_metadata_after_user_turn(
    *,
    service: Any,
    session: Session,
    user_input: str,
) -> None:
    text = _truncate_text(user_input, SESSION_SUMMARY_MAX_CHARS)
    if not text:
        return
    updates: dict[str, str] = {"last_prompt": text}
    if not (session.first_prompt or "").strip():
        updates["first_prompt"] = text
    if not (session.title or "").strip():
        updates["title"] = build_fallback_title(session.first_prompt or text)
        updates["title_source"] = "fallback_first_prompt"
    await service.update_session_metadata(session.id, **updates)


async def prime_session_metadata_for_user_turn(
    *,
    service: Any,
    session: Session,
    messages: Sequence[Mapping[str, Any]] | None = None,
    user_input: str | None = None,
) -> None:
    text = str(user_input or "").strip()
    if not text and messages:
        text, _display, _content, _parts, _attachments, _attachment_results = _latest_user_turn(
            messages
        )
    await _update_session_metadata_after_user_turn(
        service=service,
        session=session,
        user_input=text,
    )


async def _update_session_metadata_after_assistant_turn(
    *,
    service: Any,
    session_id: str,
    assistant_text: str,
    model: str | None,
) -> None:
    summary = _truncate_text(strip_reasoning_markup(assistant_text), SESSION_SUMMARY_MAX_CHARS)
    if summary:
        await service.update_session_metadata(session_id, summary=summary)

    session = await service.get_session(session_id)
    if not session:
        return
    if (session.title_source or "").strip() != "fallback_first_prompt":
        return
    first_prompt = str(session.first_prompt or "").strip()
    if not first_prompt or not summary:
        return

    next_title = build_heuristic_title(first_prompt=first_prompt, assistant_text=summary)
    next_title_source = (
        HEURISTIC_SESSION_TITLE_SOURCE
        if next_title and next_title != (session.title or "").strip()
        else ""
    )
    if next_title and next_title != (session.title or "").strip():
        await service.update_session_metadata(
            session_id,
            title=next_title,
            title_source=next_title_source,
        )

    title_client = resolve_session_title_client()
    title_model = resolve_session_title_model(model)
    if title_client.is_available and title_model:
        asyncio.create_task(
            _refine_session_title_in_background(
                service=service,
                session_id=session_id,
                first_prompt=first_prompt,
                assistant_text=summary,
                model=title_model,
            )
        )


async def _refine_session_title_in_background(
    *,
    service: Any,
    session_id: str,
    first_prompt: str,
    assistant_text: str,
    model: str,
) -> None:
    title_client = resolve_session_title_client()
    try:
        title, _usage = await title_client.generate_title(
            model=model,
            messages=build_session_title_messages(
                first_prompt=first_prompt,
                assistant_text=assistant_text,
            ),
            timeout_ms=DEFAULT_SESSION_TITLE_TIMEOUT_MS,
        )
    except Exception:
        logger.debug("failed to refine session title", exc_info=True)
        return

    if not title or is_low_quality_title(title, first_prompt=first_prompt):
        return
    session = await service.get_session(session_id)
    if not session:
        return
    if title == (session.title or "").strip():
        return
    await service.update_session_metadata(
        session_id,
        title=title,
        title_source="ai",
    )


def _resolve_model_metadata(
    model: Optional[str],
    *,
    model_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """统一收口模型上下文配置。

    当前阶段还没把远端 /v1/models 的完整 metadata 缓存接进 runtime，
    所以这里只用默认值 + model id。后续模型目录接口上线 richer metadata
    后，只需要把这层改成真正的 resolver，compaction 逻辑本身不用再动。
    """

    if isinstance(model_metadata, Mapping):
        resolved = dict(model_metadata)
        if model and not str(resolved.get("id") or "").strip():
            resolved["id"] = model
        return dict(normalize_model_metadata(resolved))
    return dict(normalize_model_metadata({"id": model or "agent"}))


def _model_catalog_endpoint(api_base: str) -> str:
    base_url = str(api_base or "").rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/v1"):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


async def _fetch_remote_model_catalog(api_base: str, api_key: str) -> list[dict[str, Any]]:
    url = _model_catalog_endpoint(api_base)
    if not url:
        return []

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    raw_models = payload if isinstance(payload, list) else list(payload.get("data", []))
    normalized: list[dict[str, Any]] = []
    for item in raw_models:
        if isinstance(item, Mapping) or isinstance(item, str):
            normalized.append(normalize_model_metadata(item))
    return normalized


async def _resolve_runtime_model_metadata(
    model: Optional[str],
    *,
    model_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = _resolve_model_metadata(model, model_metadata=model_metadata)
    if isinstance(model_metadata, Mapping) or not model:
        return resolved

    api_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or ""
    if not api_base:
        return resolved

    api_key = os.getenv("OPENAI_API_KEY", "")
    cache_key = (api_base.rstrip("/"), api_key)
    now = time.monotonic()
    cached = _MODEL_CATALOG_CACHE.get(cache_key)
    models: list[dict[str, Any]]
    if cached and (now - cached[0]) < _MODEL_CATALOG_CACHE_TTL_SECONDS:
        models = cached[1]
    else:
        try:
            models = await _fetch_remote_model_catalog(api_base, api_key)
            _MODEL_CATALOG_CACHE[cache_key] = (now, models)
        except Exception as exc:
            logger.debug("Failed to fetch remote model metadata for %s: %s", model, exc)
            return resolved

    target = str(model).strip()
    for item in models:
        if str(item.get("id") or "").strip() == target:
            return item
    return resolved


def _normalized_conversation_messages(messages: Sequence[Dict[str, Any]]) -> list[dict[str, Any]]:
    """把不同入口的 message 形态收敛成统一内部格式。"""

    normalized_messages: list[dict[str, Any]] = []
    for message in list(messages or []):
        if isinstance(message, dict) and any(
            key in message
            for key in ("display_content", "attachments", "attachment_results", "parts")
        ):
            normalized_messages.append(
                {
                    "role": str(message.get("role") or "user"),
                    "content": str(message.get("content") or ""),
                    "display_content": str(
                        message.get("display_content") or message.get("content") or ""
                    ),
                    "parts": list(message.get("parts") or []),
                    "input_content": list(
                        message.get("input_content")
                        or canonical_input_content_from_parts(list(message.get("parts") or []))
                    ),
                    "attachments": list(message.get("attachments") or []),
                    "attachment_results": list(message.get("attachment_results") or []),
                }
            )
            continue
        normalized_messages.extend(normalize_kop_messages([message]))
    return normalized_messages


def _latest_user_turn(
    normalized_messages: Sequence[Mapping[str, Any]],
) -> tuple[
    str,
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    latest_user_message = next(
        (message for message in reversed(normalized_messages) if message.get("role") == "user"),
        {},
    )
    user_input = str(latest_user_message.get("content") or "")
    user_display_input = str(latest_user_message.get("display_content") or user_input)
    input_content = list(latest_user_message.get("input_content") or [])
    user_parts = list(latest_user_message.get("parts") or [])
    attachments = list(latest_user_message.get("attachments") or [])
    attachment_results = list(latest_user_message.get("attachment_results") or [])
    return (
        user_input,
        user_display_input,
        input_content,
        user_parts,
        attachments,
        attachment_results,
    )


def _canonical_input_messages(
    normalized_messages: Sequence[Dict[str, Any]],
) -> list[dict[str, Any]]:
    input_messages: list[dict[str, Any]] = []
    for message in normalized_messages or []:
        role = str(message.get("role") or "user")
        content = list(message.get("input_content") or [])
        if not content:
            text = str(message.get("content") or "")
            if text:
                content = [{"type": "input_text", "text": text}]
        input_messages.append({"role": role, "content": content})
    return input_messages


def _parts_include_file(parts: Sequence[dict[str, Any]]) -> bool:
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("inlineData") is not None or part.get("fileData") is not None:
            return True
    return False


def _latest_attachment_context_from_messages(
    normalized_messages: Sequence[Dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attachments: list[dict[str, Any]] = []
    attachment_results: list[dict[str, Any]] = []
    for message in normalized_messages:
        if str(message.get("role") or "user") != "user":
            continue
        message_attachments = list(message.get("attachments") or [])
        message_attachment_results = list(message.get("attachment_results") or [])
        if message_attachments or message_attachment_results:
            attachments = message_attachments
            attachment_results = message_attachment_results
    return attachments, attachment_results


def _attachment_context_from_session(
    session: Session | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    state = getattr(session, "state", None) or {}
    payload = state.get(ATTACHMENT_CONTEXT_STATE_KEY)
    if not isinstance(payload, dict):
        return [], []
    return (
        [
            compact_attachment_for_session(item)
            for item in payload.get("attachments") or []
            if isinstance(item, dict)
        ],
        [
            compact_attachment_result_for_session(item)
            for item in payload.get("attachment_results") or []
            if isinstance(item, dict)
        ],
    )


def _build_attachment_context_state_delta(
    *,
    base_state_delta: dict[str, Any] | None,
    attachments: Sequence[dict[str, Any]],
    attachment_results: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(base_state_delta or {})
    if attachments or attachment_results:
        merged[ATTACHMENT_CONTEXT_STATE_KEY] = {
            "attachments": [
                compact_attachment_for_session(item)
                for item in attachments
                if isinstance(item, dict)
            ],
            "attachment_results": [
                compact_attachment_result_for_session(item)
                for item in attachment_results
                if isinstance(item, dict)
            ],
        }
    return merged


def _resolve_effective_attachment_context(
    *,
    normalized_messages: Sequence[Dict[str, Any]],
    session: Session | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    message_attachments, message_attachment_results = _latest_attachment_context_from_messages(
        normalized_messages
    )
    if message_attachments or message_attachment_results:
        return message_attachments, message_attachment_results

    session_attachments, session_attachment_results = _attachment_context_from_session(session)
    return session_attachments, session_attachment_results


def _transcript_event_type(event: SessionEvent) -> str:
    return str(
        canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
    )


def _build_pending_user_event(
    *,
    session_id: str,
    invocation_id: str,
    user_input: str,
    user_display_input: str,
    attachments: Sequence[dict[str, Any]],
    attachment_results: Sequence[dict[str, Any]],
) -> SessionEvent:
    """构造一条未落库的用户事件，专供 compaction 预览使用。"""

    return SessionEvent.from_dict(
        {
            "id": f"preview-{uuid.uuid4()}",
            "author": "user",
            "event_type": "user_message",
            "invocationId": invocation_id,
            "content": {"role": "user", "parts": [{"text": user_display_input or user_input}]},
            "timestamp": int(time.time() * 1000),
            "metadata": {
                "agent_input": user_input,
                "attachments": [
                    compact_attachment_for_session(item) for item in attachments if item
                ],
                "attachment_results": [
                    compact_attachment_result_for_session(item)
                    for item in attachment_results
                    if item
                ],
            },
            "stateDelta": {},
        },
        session_id=session_id,
    )


def _user_event_content(
    *,
    user_input: str,
    user_display_input: str,
    input_content: Sequence[dict[str, Any]],
    user_parts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    parts = list(input_content or [])
    if not parts:
        parts = canonical_input_content_from_parts(list(user_parts or []))
    if not parts:
        text = user_display_input or user_input
        parts = [{"text": text}] if text else []
    return {"role": "user", "parts": parts}
