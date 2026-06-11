from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from ksadk.sessions.base import SessionEvent

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

DATA_URL_RE = re.compile(
    r"data:(?P<mime>[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+);base64,[A-Za-z0-9+/=_-]+"
)
BASE64_FIELD_RE = re.compile(
    r"(?P<prefix>['\"](?P<field>file_data|data|bytes|base64)['\"]\s*:\s*['\"])(?P<value>[A-Za-z0-9+/=_-]{512,})(?P<suffix>['\"])",
    re.IGNORECASE,
)


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
            f"{match.group('prefix')}[base64 {match.group('field')} omitted]"
            f"{match.group('suffix')}"
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

    return sanitize_event_text_for_context(extract_text_from_event_parts(content.get("parts") or []))


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
    if raw in {"assistant", "model"} or role in {"assistant", "model"} or author in {"assistant", "model"}:
        return "assistant_message"
    return "user_message"


def extract_text_from_event_parts(parts: List[Dict[str, Any]]) -> str:
    segments: List[str] = []
    for part in parts or []:
        if isinstance(part, dict) and part.get("text"):
            segments.append(str(part["text"]))
    return "".join(segments)


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
    checkpoints = [event for event in events if canonical_event_type(event.event_type) == "context_checkpoint"]
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

    这里没有直接照搬 Claude Code 的 LLM summarizer，而是先落一个可预测、
    可恢复的结构化摘要骨架，后续再替换成真正的 summarize agent 也不需要改
    event contract。
    """
    lines: List[str] = []
    if previous_summary:
        lines.append(previous_summary)
    lines.append("Earlier conversation summary:")
    for group in groups:
        snippets: List[str] = []
        for event in group:
            event_type = canonical_event_type(
                event.event_type,
                author=event.author,
                role=str((event.content or {}).get("role") or ""),
            )
            if event_type not in TRANSCRIPT_EVENT_TYPES or event_type == "context_checkpoint":
                continue
            text = extract_event_text(event)
            if not text:
                continue
            if event_type in {"assistant_message", "tool_call"}:
                role = "assistant"
            else:
                role = "user"
            snippets.append(f"{role}: {text[:180]}")
        if snippets:
            lines.append(" | ".join(snippets))
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

    for event in events:
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event.seq_id <= compacted_until and event_type != "context_checkpoint":
            continue
        if event_type not in TRANSCRIPT_EVENT_TYPES:
            continue
        if event_type in {"context_checkpoint", "compaction_boundary"}:
            continue

        text = extract_event_text(event)
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

        if projected and projected[-1]["role"] == role:
            projected[-1]["content"] = f"{projected[-1]['content']}\n{text}".strip()
        else:
            projected.append({"role": role, "content": text})

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
