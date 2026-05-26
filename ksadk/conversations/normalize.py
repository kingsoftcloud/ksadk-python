from __future__ import annotations

from typing import Any, Dict, List, Optional
from pathlib import Path

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


def _extract_openai_image_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str):
            return url.strip()
    return ""


def _data_url_from_inline_data(inline_data: Dict[str, Any]) -> str:
    data = str(inline_data.get("data") or "").strip()
    if not data:
        return ""
    mime_type = str(inline_data.get("mimeType") or "application/octet-stream").strip()
    return f"data:{mime_type};base64,{data}"


def _inline_data_from_data_url(url: str, *, display_name: str) -> Optional[Dict[str, Any]]:
    normalized = (url or "").strip()
    if not normalized.lower().startswith("data:") or "," not in normalized:
        return None

    metadata, data = normalized.split(",", 1)
    if ";base64" not in metadata.lower():
        return None

    mime_type = metadata[5:].split(";", 1)[0].strip() or "application/octet-stream"
    return {
        "data": data.strip(),
        "mimeType": mime_type,
        "displayName": display_name,
    }


def _file_data_from_openai_image_url(url: str, *, display_name: str) -> Optional[Dict[str, Any]]:
    normalized = (url or "").strip()
    if not normalized or normalized.lower().startswith("data:"):
        return None
    return {
        "fileUri": normalized,
        "mimeType": "image/*",
        "displayName": display_name,
    }


def _guess_mime_type(display_name: str) -> str:
    suffix = Path(display_name or "").suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".txt":
        return "text/plain"
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix == ".json":
        return "application/json"
    if suffix in {".yaml", ".yml"}:
        return "application/yaml"
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".html" or suffix == ".htm":
        return "text/html"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".zip":
        return "application/zip"
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return f"image/{'jpeg' if suffix in {'.jpg', '.jpeg'} else suffix.removeprefix('.')}"
    return "application/octet-stream"


def _openai_file_display_name(item: Dict[str, Any]) -> str:
    return str(
        item.get("filename")
        or item.get("displayName")
        or item.get("display_name")
        or "uploaded_file"
    )


def _inline_data_from_openai_file(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = item.get("file_data")
    if not isinstance(data, str) or not data.strip():
        return None
    display_name = _openai_file_display_name(item)
    return {
        "data": data.strip(),
        "mimeType": str(item.get("mime_type") or item.get("mimeType") or _guess_mime_type(display_name)),
        "displayName": display_name,
    }


def _file_data_from_openai_file(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    file_uri = item.get("file_url") or item.get("file_id") or item.get("fileUri")
    if not isinstance(file_uri, str) or not file_uri.strip():
        return None
    display_name = _openai_file_display_name(item)
    return {
        "fileUri": file_uri.strip(),
        "mimeType": str(item.get("mime_type") or item.get("mimeType") or _guess_mime_type(display_name)),
        "displayName": display_name,
    }


def _canonical_input_content_from_part_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = payload.get("text")
    if text is not None:
        return {"type": "input_text", "text": str(text or "")}

    inline_data = payload.get("inlineData")
    if isinstance(inline_data, dict):
        data = str(inline_data.get("data") or "").strip()
        if not data:
            return None
        display_name = str(inline_data.get("displayName") or "uploaded_file")
        mime_type = str(inline_data.get("mimeType") or _guess_mime_type(display_name)).strip()
        if mime_type.startswith("image/"):
            return {
                "type": "input_image",
                "image_url": _data_url_from_inline_data(inline_data),
            }
        return {
            "type": "input_file",
            "filename": display_name,
            "file_data": data,
        }

    file_data = payload.get("fileData")
    if isinstance(file_data, dict):
        file_uri = str(file_data.get("fileUri") or "").strip()
        display_name = str(file_data.get("displayName") or file_uri or "uploaded_file")
        mime_type = str(file_data.get("mimeType") or _guess_mime_type(display_name)).strip()
        if mime_type.startswith("image/") and file_uri:
            return {
                "type": "input_image",
                "image_url": file_uri,
            }
        block: Dict[str, Any] = {
            "type": "input_file",
            "filename": display_name,
        }
        if file_uri:
            block["file_url"] = file_uri
        return block

    return None


def _canonical_input_content_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    item_type = str(item.get("type") or "").strip()
    if item_type in {"input_text", "text"} or (
        not item_type and item.get("text") is not None
    ):
        return {"type": "input_text", "text": str(item.get("text") or "")}

    if item_type in {"input_image", "image_url"}:
        image_url = _extract_openai_image_url(item.get("image_url"))
        if not image_url:
            return None
        block: Dict[str, Any] = {"type": "input_image", "image_url": image_url}
        if item.get("detail") is not None:
            block["detail"] = item.get("detail")
        return block

    if item_type == "input_file":
        block = {"type": "input_file"}
        filename = item.get("filename") or item.get("displayName") or item.get("display_name")
        if filename is not None:
            block["filename"] = str(filename)
        if item.get("file_data") is not None:
            block["file_data"] = str(item.get("file_data") or "")
        if item.get("file_url") is not None:
            block["file_url"] = str(item.get("file_url") or "")
        elif item.get("fileUri") is not None:
            block["file_url"] = str(item.get("fileUri") or "")
        if item.get("file_id") is not None:
            block["file_id"] = str(item.get("file_id") or "")
        if len(block) > 1:
            return block

    legacy_block = _canonical_input_content_from_part_payload(item)
    if legacy_block is not None:
        return legacy_block
    return None


def canonical_input_content_from_parts(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    canonical: List[Dict[str, Any]] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        block = _canonical_input_content_from_part_payload(part)
        if block is not None:
            canonical.append(block)
    return canonical


def canonical_input_content_from_message_content(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, list):
        canonical: List[Dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            block = _canonical_input_content_from_item(item)
            if block is not None:
                canonical.append(block)
        return canonical
    text = str(content or "")
    return [{"type": "input_text", "text": text}] if text else []


def _part_payload_from_content_item(item: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if item.get("text") is not None:
        payload["text"] = str(item.get("text") or "")
    if item.get("inlineData") is not None:
        payload["inlineData"] = item.get("inlineData")
    if item.get("fileData") is not None:
        payload["fileData"] = item.get("fileData")

    item_type = str(item.get("type") or "").strip()
    if item_type == "input_image" and "inlineData" not in payload and "fileData" not in payload:
        display_name = str(item.get("displayName") or item.get("display_name") or "uploaded_image")
        image_url = _extract_openai_image_url(item.get("image_url"))
        inline_data = _inline_data_from_data_url(image_url, display_name=display_name)
        if inline_data is not None:
            payload["inlineData"] = inline_data
        else:
            file_data = _file_data_from_openai_image_url(image_url, display_name=display_name)
            if file_data is not None:
                payload["fileData"] = file_data
    if item_type == "image_url" and "inlineData" not in payload and "fileData" not in payload:
        display_name = str(item.get("displayName") or item.get("display_name") or "uploaded_image")
        image_url = _extract_openai_image_url(item.get("image_url"))
        inline_data = _inline_data_from_data_url(image_url, display_name=display_name)
        if inline_data is not None:
            payload["inlineData"] = inline_data
        else:
            file_data = _file_data_from_openai_image_url(image_url, display_name=display_name)
            if file_data is not None:
                payload["fileData"] = file_data
    if item_type == "input_file" and "inlineData" not in payload and "fileData" not in payload:
        inline_data = _inline_data_from_openai_file(item)
        if inline_data is not None:
            payload["inlineData"] = inline_data
        else:
            file_data = _file_data_from_openai_file(item)
            if file_data is not None:
                payload["fileData"] = file_data

    return payload


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
    input_content = canonical_input_content_from_message_content(content)
    if isinstance(content, list):
        parts: List[Part] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            payload = _part_payload_from_content_item(item)
            if not payload:
                continue
            try:
                parts.append(Part.model_validate(payload))
            except Exception:
                continue
        normalized = normalize_parts_content(parts)
        normalized["input_content"] = input_content
        return normalized
    text = str(content or "")
    return {
        "content": text,
        "display_content": text,
        "parts": [{"text": text}] if text else [],
        "input_content": input_content,
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
                "input_content": list(normalized_content.get("input_content") or []),
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
                "input_content": [{"type": "input_text", "text": input_payload}] if input_payload else [],
                "attachments": [],
                "attachment_results": [],
            }
        ]
    if isinstance(input_payload, list):
        return normalize_kop_messages(input_payload)
    return []
