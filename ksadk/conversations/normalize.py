from __future__ import annotations

from typing import Any, Dict, List, Optional

from ksadk.conversations.attachments import (
    build_attachment_prompt_text,
    build_attachment_results,
    compact_attachment_result_for_session,
    decode_inline_data,
    extract_pdf_text,
    is_textual_mime,
    looks_like_textual_attachment,
    read_attachment_bytes,
    resolve_attachment_storage_path,
    resolve_uploads_dir,
)
from ksadk.conversations.context import canonical_event_type
from ksadk.server.api_models import Part


def attachment_prompt_text(attachment: Dict[str, Any]) -> str:
    result = build_attachment_results([attachment])[0]
    return build_attachment_prompt_text(result)


def extract_user_input_from_parts(parts: List[Part]) -> str:
    return str(normalize_parts_content(parts).get("content") or "")


def compact_attachment_for_session(attachment: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in attachment.items()
        if key in {"display_name", "mime_type", "transport", "size_bytes", "is_text", "file_uri"}
        and value not in (None, "")
    }


def attachment_from_part(part: Part) -> Optional[Dict[str, Any]]:
    inline = part.inlineData
    if inline and inline.data:
        display_name = inline.displayName or "uploaded_file"
        mime_type = (inline.mimeType or "").strip() or "application/octet-stream"
        try:
            raw = decode_inline_data(inline.data)
        except Exception:
            raw = b""
        return {
            "display_name": display_name,
            "mime_type": mime_type,
            "transport": "inline",
            "data": inline.data.strip(),
            "is_text": looks_like_textual_attachment(mime_type, display_name),
            "size_bytes": len(raw),
        }

    file_data = part.fileData
    if file_data and (file_data.fileUri or file_data.displayName):
        display_name = file_data.displayName or file_data.fileUri or "uploaded_file"
        mime_type = (file_data.mimeType or "").strip() or "application/octet-stream"
        storage_path = resolve_attachment_storage_path(file_data.fileUri or "")
        try:
            size_bytes = storage_path.stat().st_size if storage_path and storage_path.exists() else None
        except OSError:
            size_bytes = None
        return {
            "display_name": display_name,
            "mime_type": mime_type,
            "transport": "reference",
            "file_uri": file_data.fileUri or "",
            "is_text": looks_like_textual_attachment(mime_type, display_name),
            "size_bytes": size_bytes,
            "storage_path": str(storage_path) if storage_path else None,
        }

    return None


def display_content_from_parts(parts: List[Part]) -> str:
    text_segments: List[str] = []
    attachment_names: List[str] = []

    for part in parts or []:
        if part.text:
            text_segments.append(part.text)
            continue

        inline = part.inlineData
        if inline and (inline.displayName or inline.data):
            attachment_names.append(inline.displayName or "uploaded_file")
            continue

        file_data = part.fileData
        if file_data and (file_data.displayName or file_data.fileUri):
            attachment_names.append(file_data.displayName or file_data.fileUri or "uploaded_file")

    blocks = [segment.strip() for segment in text_segments if str(segment).strip()]
    if attachment_names:
        attachment_block = "## 附件\n" + "\n".join(f"- {name}" for name in attachment_names)
        blocks.append(attachment_block)
    return "\n\n".join(blocks).strip()


def normalize_parts_content(parts: List[Part]) -> dict[str, Any]:
    attachments = [attachment for attachment in (attachment_from_part(part) for part in parts) if attachment]
    attachment_results = build_attachment_results(attachments)
    display_content = display_content_from_parts(parts)
    segments: List[str] = []
    attachment_index = 0
    for part in parts or []:
        if part.text:
            segments.append(part.text)
            continue
        if attachment_index < len(attachment_results):
            segments.append(build_attachment_prompt_text(attachment_results[attachment_index]))
            attachment_index += 1
    return {
        "content": "\n\n".join(segment.strip() for segment in segments if str(segment).strip()).strip(),
        "display_content": display_content,
        "parts": [part.model_dump(exclude_none=True) for part in parts],
        "attachments": attachments,
        "attachment_results": attachment_results,
    }


def normalize_kop_message_content(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        parts: List[Part] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            payload: Dict[str, Any] = {}
            if item.get("text") is not None:
                payload["text"] = str(item.get("text") or "")
            if item.get("inlineData") is not None:
                payload["inlineData"] = item.get("inlineData")
            if item.get("fileData") is not None:
                payload["fileData"] = item.get("fileData")
            if not payload:
                continue
            try:
                parts.append(Part.model_validate(payload))
            except Exception:
                continue
        return normalize_parts_content(parts)
    text = str(content or "")
    return {
        "content": text,
        "display_content": text,
        "parts": [{"text": text}] if text else [],
        "attachments": [],
        "attachment_results": [],
    }


def normalize_kop_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for message in messages or []:
        role = str(message.get("role") or "user")
        normalized_content = normalize_kop_message_content(message.get("content", ""))
        normalized.append(
            {
                "role": role,
                "content": str(normalized_content.get("content") or ""),
                "display_content": str(normalized_content.get("display_content") or ""),
                "parts": list(normalized_content.get("parts") or []),
                "attachments": list(normalized_content.get("attachments") or []),
                "attachment_results": list(normalized_content.get("attachment_results") or []),
            }
        )
    return normalized


def normalize_responses_input(input_payload: Any) -> List[Dict[str, Any]]:
    if isinstance(input_payload, str):
        return [
            {
                "role": "user",
                "content": input_payload,
                "display_content": input_payload,
                "parts": [{"text": input_payload}] if input_payload else [],
                "attachments": [],
                "attachment_results": [],
            }
        ]
    if isinstance(input_payload, list):
        return normalize_kop_messages(input_payload)
    return []
