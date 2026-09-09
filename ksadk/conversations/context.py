from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Iterable, List

from ksadk.events.canonical import ItemCompleted, parse_runtime_event
from ksadk.events.content import TextContent
from ksadk.sessions.base import SessionEvent
from ksadk.tools.result_budget import (
    ToolResultBudget,
    budget_tool_output,
    default_tool_result_budget,
)

CANONICAL_EVENT_TYPES = {
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "approval_request",
    "approval_response",
    "attachment_ref",
    "reasoning",
    "run_status",
    "run_checkpoint",
    "run_resume",
    "context_checkpoint",
    "compaction_boundary",
}

TRANSCRIPT_EVENT_TYPES = {
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "approval_request",
    "approval_response",
    "attachment_ref",
    "context_checkpoint",
}

_RUNTIME_PLACEHOLDER_EVENT_TYPES = {
    "tool_call",
    "tool_result",
    "approval_request",
    "approval_response",
}

DATA_URL_RE = re.compile(r"data:(?P<mime>[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+);base64,[A-Za-z0-9+/=_-]+")
BASE64_FIELD_RE = re.compile(
    r"(?P<prefix>['\"](?P<field>file_data|data|bytes|base64)['\"]\s*:\s*['\"])(?P<value>[A-Za-z0-9+/=_-]{512,})(?P<suffix>['\"])",
    re.IGNORECASE,
)
_CORRECTION_MARKER_RE = re.compile(
    r"(?:修正|更正|改为|更新为|最新(?:的)?|废弃|作废|不再使用|不是.+而是|不要|不得|禁止)"
)
_LATEST_USER_INSTRUCTION_MAX_CHARS = 8192
_CORRECTION_SUMMARY_MAX_CHARS = 2048


def sanitize_event_text_for_context(text: Any) -> str:
    """Return a compact text view for history/compaction without inline binaries."""
    value = str(text or "")
    if not value:
        return ""

    def _replace_data_url(match: re.Match[str]) -> str:
        mime = match.group("mime")
        media_type = "image" if mime.startswith("image/") else "file"
        return f"[{media_type}: {mime} data-url omitted]"

    value = DATA_URL_RE.sub(_replace_data_url, value)
    value = BASE64_FIELD_RE.sub(
        lambda match: (
            f"{match.group('prefix')}[base64 {match.group('field')} omitted]{match.group('suffix')}"
        ),
        value,
    )
    return value


def extract_event_text(event: SessionEvent) -> str:
    """从结构化事件里提取最适合喂给模型的文本视图。

    这里优先取 `agent_input`，因为它通常比展示给 UI 的 display text 更接近
    真实 prompt；如果没有，再退回到 parts/text。
    """
    metadata = event.metadata or {}
    if metadata.get("agent_input"):
        return sanitize_event_text_for_context(metadata["agent_input"])

    content = event.content or {}
    text = content.get("text")
    if text:
        return sanitize_event_text_for_context(text)

    return sanitize_event_text_for_context(
        extract_text_from_event_parts(content.get("parts") or [])
    )


def _canonical_completed_message(
    event: SessionEvent,
) -> tuple[str, str, tuple[str, str]] | None:
    """Return one canonical assistant message without flattening its identity.

    Canonical RuntimeEvents are persisted inside the existing ``SessionEvent``
    carrier.  Legacy transcript projection only inspects carrier-level fields,
    so an ``item.completed`` message otherwise disappears from the next turn's
    history.  The canonical event id and ``(scope_id, item_id)`` are both kept:
    replays are idempotent, while equal text from different items remains
    distinct.
    """

    raw = (event.content or {}).get("runtime_event")
    if not isinstance(raw, Mapping):
        return None
    try:
        canonical = parse_runtime_event(dict(raw))
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(canonical, ItemCompleted)
        or canonical.item_kind != "message"
        or event.event_type != canonical.event_type
    ):
        return None
    text = sanitize_event_text_for_context(
        "".join(
            part.text for part in canonical.snapshot.parts if isinstance(part, TextContent)
        )
    )
    if not text:
        return None
    return canonical.event_id, text, (canonical.scope_id, canonical.item_id)


def _transcript_event_projection(
    event: SessionEvent,
) -> tuple[str, str, tuple[str, tuple[str, str]] | None]:
    canonical_message = _canonical_completed_message(event)
    if canonical_message is not None:
        event_id, text, item_identity = canonical_message
        return "assistant_message", text, (event_id, item_identity)
    return (
        canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        ),
        extract_event_text(event),
        None,
    )


def canonical_event_type(
    event_type: str | None,
    *,
    author: str = "",
    role: str = "",
) -> str:
    raw = str(event_type or "").strip().lower()
    if raw in CANONICAL_EVENT_TYPES:
        return raw
    if raw in {"tool_use", "function_call"}:
        return "tool_call"
    if raw in {"tool_response", "function_response"}:
        return "tool_result"
    if raw in {"approval", "interrupt"}:
        return "approval_request"
    if raw in {"attachment", "file_ref", "file_reference"}:
        return "attachment_ref"
    if raw in {"checkpoint", "context_checkpoint"}:
        return "context_checkpoint"
    if raw in {"boundary", "compaction_boundary"}:
        return "compaction_boundary"
    if raw in {"status", "run_status"}:
        return "run_status"
    if raw in {"run_checkpoint", "runtime_checkpoint"}:
        return "run_checkpoint"
    if raw in {"run_resume", "runtime_resume"}:
        return "run_resume"
    if (
        raw in {"assistant", "model"}
        or role in {"assistant", "model"}
        or author in {"assistant", "model"}
    ):
        return "assistant_message"
    return "user_message"


def extract_text_from_event_parts(parts: List[Dict[str, Any]]) -> str:
    segments: List[str] = []
    for part in parts or []:
        if isinstance(part, dict) and part.get("text"):
            segments.append(_stringify_part_text(part["text"]))
    return "\n".join(segments)


def _stringify_part_text(value: Any) -> str:
    if isinstance(value, dict):
        preview = (
            value.get("stdout")
            or value.get("stderr")
            or value.get("text")
            or value.get("content")
            or value.get("preview")
            or ""
        )
        persisted = value.get("persisted")
        if isinstance(persisted, dict) and persisted.get("path"):
            mime_type = str(persisted.get("mime_type") or "text/plain")
            suffix = f"\n[persisted-output] {persisted['path']} ({mime_type})"
            return f"{preview}{suffix}".strip()
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def budget_tool_result_for_event(
    *,
    tool_name: str,
    tool_output: Any,
    tool_call_id: str | None,
    enabled: bool,
    budget: ToolResultBudget | None = None,
) -> tuple[str, dict[str, Any]]:
    """PR C：tool_result 落 SessionEvent 前的单项预算（ksadk_hosted 门控）。

    返回 ``(session_event_text, metadata_extras)``：

    - ``enabled=False`` → ``(str(tool_output), {})``：与旧
      ``text=str(tool_output)`` **字节级一致**，非ksadk_hosted / framework / native
      路径零行为变更。
    - ``enabled=True``：先 ``_stringify_part_text`` 干净渲染（已预算的 toolset dict →
      ``"preview\\n[persisted-output] path (mime)"``；裸串 → 原串），再若仍超 ``max_chars`` 则
      ``budget_tool_output`` 落盘+截断，``text = "preview\\n[persisted-output] path (mime)"``，
      ``extras = {"tool_result_budget": {truncated, original_chars, preview_chars, persisted}}``。
      未超阈值 → ``(rendered, {})``。

    **不碰 ``metadata.tool_output``**：调用方保留原值（UI/Responses 读取方不受影响，
    会话存储节省留后续）。
    只 bound 进 ``content.parts[0].text``——即下一轮 ``extract_event_text`` → ``payload["history"]``
    → 模型输入的那条 text。已预算 dict 经 ``_stringify_part_text`` 渲染后必小于阈值，不重复落盘。
    """
    if not enabled:
        return str(tool_output), {}
    active = budget or default_tool_result_budget()
    rendered = _stringify_part_text(tool_output)
    if len(rendered) <= active.max_chars:
        return rendered, {}
    budgeted = budget_tool_output(
        tool_name=tool_name,
        field_name="output",
        value=tool_output,
        metadata={"tool_call_id": tool_call_id or ""},
        budget=active,
    )
    preview = str(budgeted.get("output") or "")
    persisted = budgeted.get("persisted")
    if not isinstance(persisted, Mapping) or not persisted.get("path"):
        # 无落盘（不应发生，但兜底）→ 退回 rendered 截断标记，不谎报 persisted。
        marker = f"\n[truncated {len(rendered) - active.max_chars} chars]"
        return (rendered[: active.max_chars] + marker), {
            "tool_result_budget": {
                "truncated": True,
                "original_chars": int(budgeted.get("original_chars") or len(rendered)),
                "preview_chars": active.max_chars,
            }
        }
    mime_type = persisted.get("mime_type") or "text/plain"
    text = f"{preview}\n[persisted-output] {persisted['path']} ({mime_type})"
    extras = {
        "tool_result_budget": {
            "truncated": bool(budgeted.get("truncated")),
            "original_chars": int(budgeted.get("original_chars") or 0),
            "preview_chars": int(budgeted.get("preview_chars") or len(preview)),
            "persisted": dict(persisted),
        }
    }
    return text, extras


def build_request_history(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    history: List[Dict[str, str]] = []
    for message in messages or []:
        role = str(message.get("role") or "")
        if role in {"assistant", "model"}:
            role = "model"
        elif role not in {"user", "model"}:
            continue
        content = str(message.get("content") or "")
        if content:
            history.append({"role": role, "content": content})
    return history


def compacted_until_seq_id(events: List[SessionEvent]) -> int:
    """读取最新 checkpoint 覆盖到哪一个 seq_id。"""
    checkpoints = [
        event for event in events if canonical_event_type(event.event_type) == "context_checkpoint"
    ]
    if not checkpoints:
        return 0
    latest = checkpoints[-1]
    return int((latest.metadata or {}).get("compacted_until_seq_id") or 0)


def group_events_by_api_round(events: List[SessionEvent]) -> List[List[SessionEvent]]:
    """按 invocation/轮次分组，保证压缩和 PTL 截断不会打断一整轮对话。"""
    groups: List[List[SessionEvent]] = []
    current_key: str | None = None
    for event in events:
        key = str(event.invocation_id or f"seq:{event.seq_id}")
        if key != current_key:
            groups.append([])
            current_key = key
        groups[-1].append(event)
    return groups


def summarize_event_groups(
    groups: List[List[SessionEvent]],
    *,
    previous_summary: str = "",
) -> str:
    """把要折叠的旧轮次压成一段 checkpoint 文本。

    extractive fallback：结构化骨架 + 有界保留错误修正和最新长 user 指令。
    无摘要模型时，关键修正可能位于长消息尾部，也可能不是最后一条 user 消息；
    因此跨消息提取修正，并在预算上限内保留最新长指令首尾（方案 §9.4）。
    """
    lines: List[str] = []
    seen_canonical_event_ids: set[str] = set()
    seen_canonical_items: set[tuple[str, str]] = set()
    if previous_summary:
        lines.append(previous_summary)
    lines.append("Earlier conversation summary:")
    last_user_text = ""
    correction_snippets: list[str] = []
    for group in groups:
        snippets: List[str] = []
        for event in group:
            event_type, text, canonical_identity = _transcript_event_projection(event)
            if canonical_identity is not None:
                event_id, item_identity = canonical_identity
                if (
                    event_id in seen_canonical_event_ids
                    or item_identity in seen_canonical_items
                ):
                    continue
                seen_canonical_event_ids.add(event_id)
                seen_canonical_items.add(item_identity)
            if event_type not in TRANSCRIPT_EVENT_TYPES or event_type == "context_checkpoint":
                continue
            if not text:
                continue
            if event_type in {"assistant_message", "tool_call"}:
                role = "assistant"
            else:
                role = "user"
                last_user_text = text
                for sentence in re.split(r"[\n。；;]+", text):
                    normalized = sentence.strip()
                    if normalized and _CORRECTION_MARKER_RE.search(normalized):
                        correction_snippets.append(normalized[:512])
            snippets.append(f"{role}: {text[:180]}")
        if snippets:
            lines.append(" | ".join(snippets))
    # 修正不一定是 compact 范围内最后一条 user 消息；单独形成结构化段，供
    # Working State 确定性解析。总量有界，避免用“保留完整”重新撑爆上下文。
    if correction_snippets:
        unique: list[str] = []
        for snippet in correction_snippets:
            if snippet not in unique:
                unique.append(snippet)
        correction_text = "；".join(unique[-8:])[-_CORRECTION_SUMMARY_MAX_CHARS:]
        lines.append(f"错误修正：{correction_text}")
    # 末尾追加最新 user 指令的有界首尾内容。短消息已在摘要骨架里，不重复。
    if last_user_text and len(last_user_text) > 180:
        preserved = last_user_text
        if len(preserved) > _LATEST_USER_INSTRUCTION_MAX_CHARS:
            half = _LATEST_USER_INSTRUCTION_MAX_CHARS // 2
            preserved = (
                preserved[:half]
                + "\n...[中间内容因上下文预算省略]...\n"
                + preserved[-half:]
            )
        lines.append(f"最新用户指令（有界保留）: {preserved}")
    return "\n".join(line for line in lines if line).strip()


def project_model_messages(
    events: List[SessionEvent],
    *,
    assistant_role: str = "model",
) -> List[Dict[str, str]]:
    """把 append-only transcript 投影成运行时 history。

    核心原则：
    1. `context_checkpoint` 之前的正文不再重复展开。
    2. `run_status` 这类 transport/control 事件不进入模型上下文。
    3. tool/approval/attachment 仍保留成可解释的文本占位，避免状态丢失。
    """
    projected: List[Dict[str, str]] = []
    placeholder_flags: list[bool] = []
    identity_boundary_flags: list[bool] = []
    seen_canonical_event_ids: set[str] = set()
    seen_canonical_items: set[tuple[str, str]] = set()
    compacted_until = compacted_until_seq_id(events)
    checkpoint = next(
        (
            event
            for event in reversed(events)
            if canonical_event_type(event.event_type) == "context_checkpoint"
        ),
        None,
    )
    if checkpoint:
        summary_text = extract_event_text(checkpoint)
        if summary_text:
            projected.append(
                {
                    "role": assistant_role,
                    "content": summary_text,
                }
            )
            placeholder_flags.append(False)
            identity_boundary_flags.append(False)

    for event in events:
        event_type, text, canonical_identity = _transcript_event_projection(event)
        if event.seq_id <= compacted_until and event_type != "context_checkpoint":
            continue
        if event_type not in TRANSCRIPT_EVENT_TYPES:
            continue
        if event_type in {"context_checkpoint", "compaction_boundary"}:
            continue
        if canonical_identity is not None:
            event_id, item_identity = canonical_identity
            if (
                event_id in seen_canonical_event_ids
                or item_identity in seen_canonical_items
            ):
                continue
            seen_canonical_event_ids.add(event_id)
            seen_canonical_items.add(item_identity)

        if not text:
            continue

        if event_type == "assistant_message":
            role = assistant_role
        elif event_type == "tool_call":
            role = assistant_role
            text = f"[tool_call] {text}"
        elif event_type == "tool_result":
            role = "user"
            text = f"[tool_result] {text}"
        elif event_type == "approval_request":
            role = assistant_role
            text = f"[approval_request] {text}"
        elif event_type == "approval_response":
            role = "user"
            text = f"[approval_response] {text}"
        elif event_type == "attachment_ref":
            role = "user"
            text = f"[attachment] {text}"
        else:
            role = "user"

        is_placeholder = event_type in _RUNTIME_PLACEHOLDER_EVENT_TYPES
        if (
            projected
            and projected[-1]["role"] == role
            and not placeholder_flags[-1]
            and not identity_boundary_flags[-1]
            and not is_placeholder
            and canonical_identity is None
        ):
            projected[-1]["content"] = f"{projected[-1]['content']}\n{text}".strip()
        else:
            projected.append({"role": role, "content": text})
            placeholder_flags.append(is_placeholder)
            identity_boundary_flags.append(canonical_identity is not None)

    return projected


def build_history_from_events(events: List[SessionEvent]) -> List[Dict[str, str]]:
    """本地 runner 使用的最终 history 视图。"""
    history: List[Dict[str, str]] = []
    for message in project_model_messages(events, assistant_role="model"):
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role in {"user", "model"} and content:
            history.append({"role": role, "content": content})
    return history


def _responses_json_string(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value or default
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _responses_message(role: str, text: Any) -> dict[str, Any] | None:
    normalized_text = sanitize_event_text_for_context(text).strip()
    if not normalized_text:
        return None
    return {
        "role": role,
        "content": [{"type": "input_text", "text": normalized_text}],
    }


def _event_content_function_part(event: SessionEvent, part_name: str) -> Mapping[str, Any] | None:
    content = event.content or {}
    parts = content.get("parts") or []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        value = part.get(part_name)
        if isinstance(value, Mapping):
            return value
    return None


def _response_tool_call_id(event: SessionEvent) -> str:
    metadata = event.metadata or {}
    for key in ("tool_call_id", "call_id", "run_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    receipt = metadata.get("tool_receipt")
    if isinstance(receipt, Mapping):
        for key in ("tool_call_id", "call_id", "run_id"):
            value = receipt.get(key)
            if value:
                return str(value)
    resume_input = metadata.get("resume_input")
    if isinstance(resume_input, Mapping):
        for key in ("tool_call_id", "call_id", "run_id"):
            value = resume_input.get(key)
            if value:
                return str(value)
    return ""


def _response_tool_call_item(event: SessionEvent) -> dict[str, Any] | None:
    metadata = event.metadata or {}
    content = event.content or {}
    function_call = _event_content_function_part(event, "function_call") or {}
    name = str(
        metadata.get("tool_name")
        or content.get("name")
        or content.get("tool_name")
        or function_call.get("name")
        or ""
    ).strip()
    arguments: Any = metadata.get("tool_args")
    if arguments is None:
        arguments = (
            content.get("arguments")
            or content.get("args")
            or content.get("input")
            or function_call.get("args")
            or function_call.get("arguments")
            or {}
        )
    call_id = _response_tool_call_id(event)
    if not name or not call_id:
        return None
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": _responses_json_string(arguments, default="{}"),
    }


def _response_tool_output_item(event: SessionEvent) -> dict[str, Any] | None:
    metadata = event.metadata or {}
    content = event.content or {}
    function_response = _event_content_function_part(event, "function_response") or {}
    output: Any = metadata.get("tool_output") if "tool_output" in metadata else None
    if output is None:
        resume_input = metadata.get("resume_input")
        if isinstance(resume_input, Mapping) and "output" in resume_input:
            output = resume_input.get("output")
    if output is None:
        output = (
            content.get("output")
            or content.get("result")
            or function_response.get("response")
            or extract_event_text(event)
        )
    call_id = _response_tool_call_id(event)
    if not call_id:
        return None
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": _responses_json_string(output),
    }


def project_responses_history(events: List[SessionEvent]) -> List[dict[str, Any]]:
    """Project the compacted transcript into OpenAI Responses input items.

    The checkpoint summary represents the compacted prefix. Only the retained
    tail is replayed as typed tool items, so an old call_id is never fabricated
    from a text summary. Events without a reliable call id fall back to the
    existing explanatory message representation instead of emitting an invalid
    ``function_call_output`` item.

    公开承诺（契约声明见 ``ksadk/events/projections.py``）：仅 OpenAI Responses
    input item 形态（``type``/``call_id``/``output``/role 消息）；内部不保证
    compacted 前缀的重放方式与占位消息的具体措辞。
    """
    projected: List[dict[str, Any]] = []
    projected_call_ids: set[str] = set()
    seen_canonical_event_ids: set[str] = set()
    seen_canonical_items: set[tuple[str, str]] = set()
    compacted_until = compacted_until_seq_id(events)
    checkpoint = next(
        (
            event
            for event in reversed(events)
            if canonical_event_type(event.event_type) == "context_checkpoint"
        ),
        None,
    )
    if checkpoint:
        summary_message = _responses_message("assistant", extract_event_text(checkpoint))
        if summary_message:
            projected.append(summary_message)

    for event in events:
        event_type, text, canonical_identity = _transcript_event_projection(event)
        if event.seq_id <= compacted_until and event_type != "context_checkpoint":
            continue
        if event_type not in TRANSCRIPT_EVENT_TYPES:
            continue
        if event_type in {"context_checkpoint", "compaction_boundary"}:
            continue
        if canonical_identity is not None:
            event_id, item_identity = canonical_identity
            if (
                event_id in seen_canonical_event_ids
                or item_identity in seen_canonical_items
            ):
                continue
            seen_canonical_event_ids.add(event_id)
            seen_canonical_items.add(item_identity)

        if event_type == "tool_call":
            item = _response_tool_call_item(event)
            if item:
                projected.append(item)
                projected_call_ids.add(str(item["call_id"]))
                continue
            message = _responses_message("assistant", f"[tool_call] {text}")
        elif event_type == "tool_result":
            item = _response_tool_output_item(event)
            if item and str(item["call_id"]) in projected_call_ids:
                projected.append(item)
                continue
            message = _responses_message("user", f"[tool_result] {text}")
        elif event_type == "assistant_message":
            message = _responses_message("assistant", text)
        elif event_type == "approval_request":
            message = _responses_message(
                "assistant", f"[approval_request] {text}"
            )
        elif event_type == "approval_response":
            message = _responses_message("user", f"[approval_response] {text}")
        elif event_type == "attachment_ref":
            message = _responses_message("user", f"[attachment] {text}")
        else:
            message = _responses_message("user", text)
        if message:
            projected.append(message)

    return projected


def build_responses_history_from_messages(
    messages: Sequence[Mapping[str, Any]],
) -> List[dict[str, Any]]:
    """Normalize request-provided prior messages for Responses history."""
    history: List[dict[str, Any]] = []
    for message in messages or []:
        role = str(message.get("role") or "").strip().lower()
        if role == "model":
            role = "assistant"
        if role not in {"user", "assistant"}:
            continue

        content = message.get("input_content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
            content = message.get("content")
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            content_items = [dict(item) for item in content if isinstance(item, Mapping)]
            if content_items:
                history.append({"role": role, "content": content_items})
                continue

        item = _responses_message(role, message.get("content"))
        if item:
            history.append(item)
    return history
