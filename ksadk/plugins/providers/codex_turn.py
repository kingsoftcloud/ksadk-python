"""Provider-owned projection of bound capabilities and canonical input into Codex.

One projector belongs to one native adapter. Attachment persistence retains the
existing native-thread continuation contract; adapter close is not session close.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from ksadk.codex.projection import ProjectedCodexTurn
from ksadk.runtime.adapter import ResumePayload, StartRequest


class CodexTurnProjector:
    def __init__(
        self, *, bound_skill_paths: Mapping[str, str] | None = None,
        enforce_bound_skills: bool = False,
    ) -> None:
        self._skills = dict(bound_skill_paths or {})
        self._enforce_bound_skills = enforce_bound_skills
        self._closed = False

    def prepare(self, request: StartRequest) -> StartRequest:
        """Project admitted native Skill paths without mutating a caller request."""
        self._require_open()
        skills = request.config.get("skills")
        if self._enforce_bound_skills:
            if skills is not None and (not isinstance(skills, list) or any(
                not isinstance(item, dict)
                or str(item.get("name") or "").strip() not in self._skills
                for item in skills
            )):
                raise ValueError("Codex request contains a Skill absent from the immutable Bundle")
            return request.model_copy(update={"config": {
                **request.config,
                "skills": [{"name": name, "path": path} for name, path in self._skills.items()],
            }})
        if not self._skills or not isinstance(skills, list):
            return request
        projected = [
            {**item, "path": self._skills.get(
                str(item.get("name") or "").strip(), str(item.get("path") or ""),
            )}
            for item in skills if isinstance(item, dict)
        ]
        return request.model_copy(update={"config": {**request.config, "skills": projected}})

    def project(
        self, request: StartRequest | None, payload: ResumePayload | None,
    ) -> ProjectedCodexTurn:
        self._require_open()
        prompt = _request_prompt(request) if request is not None else _resume_prompt(payload)
        return ProjectedCodexTurn(_build_run_input(request, prompt, self._materialize_file))

    def close(self) -> None:
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Codex turn projector is closed")

    def _materialize_file(self, data_url: str, filename: str) -> Path | None:
        """Materialize a bounded Studio inline attachment for native Codex."""

        match = re.fullmatch(r"data:([^;,]+)?;base64,([A-Za-z0-9+/=\s]+)", data_url)
        encoded = match.group(2) if match is not None else data_url.strip()
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return None
        if not payload or len(payload) > 10 * 1024 * 1024:
            return None
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).name).strip(".-")
        safe_name = safe_name[:120] or "attachment"
        digest = hashlib.sha256(payload).hexdigest()
        # Native thread continuation can outlive this adapter. Keep the existing
        # content-addressed storage until the session owner supplies retention.
        path = Path("/tmp/ksadk-codex-attachments") / digest / safe_name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(payload)
        return path


def _resume_prompt(payload: Optional[ResumePayload]) -> Any:
    if payload is None:
        return ""
    if isinstance(payload.data, str):
        return payload.data
    return json.dumps(payload.data, ensure_ascii=False, sort_keys=True)


def _coerce_prompt_text(value: Any) -> Any:
    """把 canonical message 形态的 input 压成 SDK 可接受的文本。

    ``openai-codex`` 0.147 的 run input 只接受 TextInput/str;请求侧没有
    conversation preprocessing 时 ``request.input`` 可能是
    ``[{role, content}]`` 历史列表,直接透传会 ``unsupported input item``。
    """
    if isinstance(value, str) or value is None:
        return value
    if isinstance(value, dict):
        content = value.get("content") if "role" in value else value.get("text")
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            text = content.get("text") or content.get("content")
            if isinstance(text, str):
                return text
        return str(value)
    if isinstance(value, list):
        texts = [
            text
            for text in (_coerce_prompt_text(item) for item in value)
            if isinstance(text, str) and text
        ]
        return "\n".join(texts) if texts else str(value)
    return str(value)


def _request_prompt(request: StartRequest) -> Any:
    """Render canonical conversation history for a native Codex turn.

    只传 user 消息历史 + 当前 input，不传 assistant 的完整回复（避免 codex 复述历史）。
    assistant 回复用简短摘要代替，仅维持对话结构。
    """
    # A resumed Codex thread already owns its transcript. Re-sending Studio's
    # transport-neutral history would duplicate every prior turn after refresh.
    if str(request.metadata.get("thread_id") or "").strip():
        return (
            request.input
            if _is_structured_turn_input(request.input)
            else _coerce_prompt_text(request.input)
        )

    conversation = request.conversation_preprocessing()
    if conversation is None or not conversation.messages:
        # Keep native text/image/mention parts intact for _build_run_input().
        # Flattening this list turns an image dict into user-visible text.
        return (
            request.input
            if _is_structured_turn_input(request.input)
            else _coerce_prompt_text(request.input)
        )

    lines: list[str] = []
    for message in conversation.messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        role = str(message.get("role") or "user").strip().lower()
        if role == "assistant":
            # 不传完整回复，只标注"已回复"，避免 codex 复述
            summary = content[:80] + ("..." if len(content) > 80 else "")
            lines.append(f"[上一轮已回复: {summary}]")
        elif role == "user":
            lines.append(f"User: {content}")
    return "\n".join(lines) or request.input


def _is_structured_turn_input(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(item, dict) and isinstance(item.get("type"), str) for item in value
    )


def _build_run_input(
    request: Optional[StartRequest], prompt: Any, materialize_file: Any
) -> Any:
    """Compose Codex skills and native text/image/mention turn input."""
    try:
        from openai_codex import ImageInput, LocalImageInput, MentionInput, SkillInput, TextInput
    except ImportError:
        return prompt
    skills: list[SkillInput] = []
    if request is not None:
        raw = request.config.get("skills") if request.config else None
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                path = str(item.get("path") or "").strip()
                if name and path:
                    skills.append(SkillInput(name=name, path=path))
    native_items: list[Any] = []
    raw_input = request.input if request is not None else prompt
    if isinstance(raw_input, list):
        text_replaced = False
        for item in raw_input:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if not kind and "role" in item:
                # canonical conversation message({role, content});当前 input
                # 已由 prompt(或 conversation preprocessing)承载,跳过历史项。
                continue
            if kind in {"text", "input_text"}:
                text = str(
                    prompt
                    if not text_replaced and isinstance(prompt, str)
                    else item.get("text") or ""
                )
                text_replaced = True
                if text:
                    native_items.append(TextInput(text=text))
            elif kind in {"image", "input_image"} and (item.get("url") or item.get("image_url")):
                native_items.append(ImageInput(url=str(item.get("url") or item.get("image_url"))))
            elif kind == "localImage" and item.get("path"):
                native_items.append(LocalImageInput(path=str(item["path"])))
            elif kind == "input_file" and (
                item.get("file_data") or str(item.get("file_url") or "").startswith("data:")
            ):
                file_path = materialize_file(
                    str(item.get("file_data") or item.get("file_url")),
                    str(item.get("filename") or "attachment"),
                )
                if file_path is not None:
                    # App Server's ``mention`` input is presentation metadata:
                    # current Codex versions do not include it in the model's
                    # user message.  Always add an explicit model-visible
                    # attachment context as well.  Small textual files are
                    # inlined deterministically; binary/large files expose a
                    # sandbox-readable path that Codex can inspect with tools.
                    native_items.append(TextInput(text=_attachment_context_text(file_path, item)))
                    native_items.append(MentionInput(name=file_path.name, path=str(file_path)))
            elif kind == "mention" and item.get("path"):
                native_items.append(
                    MentionInput(
                        name=str(item.get("name") or item["path"]),
                        path=str(item["path"]),
                    )
                )
    else:
        text = prompt if isinstance(prompt, str) else str(prompt or "")
        if text:
            native_items.append(TextInput(text=text))
    combined = [*skills, *native_items]
    if not combined:
        return prompt
    if len(combined) == 1 and isinstance(combined[0], TextInput) and not skills:
        return combined[0].text
    return combined




_MAX_INLINE_ATTACHMENT_TEXT_BYTES = 64 * 1024
_TEXT_ATTACHMENT_SUFFIXES = {
    ".csv",
    ".html",
    ".htm",
    ".ini",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".rst",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def _attachment_context_text(path: Path, item: Mapping[str, Any]) -> str:
    """Build model-visible context for a materialized Responses input file."""

    name = str(item.get("filename") or path.name).replace('"', "'")
    inline_data = item.get("inlineData")
    inline_mime = inline_data.get("mimeType") if isinstance(inline_data, Mapping) else None
    mime_type = str(item.get("mime_type") or inline_mime or "").strip().lower()
    source = str(item.get("file_data") or item.get("file_url") or "")
    data_url_match = re.match(r"data:([^;,]+)", source)
    if not mime_type and data_url_match is not None:
        mime_type = data_url_match.group(1).strip().lower()
    is_text = mime_type.startswith("text/") or path.suffix.lower() in _TEXT_ATTACHMENT_SUFFIXES
    header = f'<uploaded_attachment name="{name}" path="{path}">'
    if not is_text:
        return (
            f"{header}\n"
            "The uploaded file is available at the path above. Read it with an appropriate "
            "tool before answering questions about its contents.\n"
            "</uploaded_attachment>"
        )

    raw = path.read_bytes()
    truncated = len(raw) > _MAX_INLINE_ATTACHMENT_TEXT_BYTES
    text = raw[:_MAX_INLINE_ATTACHMENT_TEXT_BYTES].decode("utf-8", errors="replace")
    suffix = "\n[attachment content truncated]" if truncated else ""
    return f"{header}\n{text}{suffix}\n</uploaded_attachment>"
