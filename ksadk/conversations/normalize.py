from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

from ksadk.conversations.context import canonical_event_type
from ksadk.server.api_models import Part
from ksadk.sessions.local_service import resolve_local_session_dir

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
    "application/pdf",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/x-ndjson",
}
_TEXT_FILE_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".tsv",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".html",
    ".css",
    ".sql",
    ".xml",
    ".sh",
}
_MAX_INLINE_BASE64_CHARS = 4_000_000
_MAX_INLINE_TEXT_CHARS = 20_000
_MAX_REFERENCE_TEXT_BYTES = 3_000_000
_UPLOAD_URI_SCHEME = "ksadk-upload://"


def is_textual_mime(mime_type: str) -> bool:
    mime = (mime_type or "").lower()
    if not mime:
        return False
    return mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIME_TYPES


def looks_like_textual_attachment(mime_type: str, display_name: str) -> bool:
    suffix = Path(display_name or "").suffix.lower()
    return is_textual_mime(mime_type) or suffix in _TEXT_FILE_EXTENSIONS


def extract_pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:
        return ""

    segments: List[str] = []
    for page in reader.pages[:10]:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text:
            segments.append(page_text)

    return "\n".join(segments).strip()


def decode_inline_data(data_b64: str) -> bytes:
    return base64.b64decode((data_b64 or "").strip() + "===")


def resolve_uploads_dir() -> Path:
    uploads_dir = resolve_local_session_dir() / "files"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return uploads_dir


def resolve_attachment_storage_path(file_uri: str) -> Optional[Path]:
    normalized_uri = (file_uri or "").strip()
    if not normalized_uri:
        return None

    if normalized_uri.startswith("local:"):
        path = Path(normalized_uri[6:]).expanduser()
        return path.resolve()

    if normalized_uri.startswith(_UPLOAD_URI_SCHEME):
        file_id = normalized_uri.removeprefix(_UPLOAD_URI_SCHEME).strip("/")
        if not file_id:
            return None

        for candidate in sorted(resolve_uploads_dir().glob(f"{file_id}*")):
            if candidate.is_file():
                return candidate.resolve()

    return None


def read_attachment_bytes(storage_path: Optional[Path], *, size_limit: Optional[int] = None) -> Optional[bytes]:
    if storage_path is None or not storage_path.is_file():
        return None

    try:
        if size_limit is not None and storage_path.stat().st_size > size_limit:
            return None
        return storage_path.read_bytes()
    except OSError:
        return None


def extract_inline_attachment_text(*, display_name: str, mime_type: str, raw: bytes) -> str:
    if mime_type == "application/pdf" or display_name.lower().endswith(".pdf"):
        text = extract_pdf_text(raw)
        if not text:
            return ""
        if len(text) > _MAX_INLINE_TEXT_CHARS:
            return text[:_MAX_INLINE_TEXT_CHARS] + "\n...[内容已截断]"
        return text

    if looks_like_textual_attachment(mime_type, display_name):
        text = raw.decode("utf-8", errors="ignore")
        if len(text) > _MAX_INLINE_TEXT_CHARS:
            return text[:_MAX_INLINE_TEXT_CHARS] + "\n...[内容已截断]"
        return text

    return ""


def attachment_prompt_text(attachment: Dict[str, Any]) -> str:
    display_name = str(attachment.get("display_name") or "uploaded_file")
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    transport = str(attachment.get("transport") or "")

    if transport == "inline":
        data_b64 = str(attachment.get("data") or "").strip()
        if len(data_b64) > _MAX_INLINE_BASE64_CHARS:
            return (
                "[上传文件: "
                f"{display_name}, "
                f"mime={mime_type or 'unknown'}, "
                "内容过大，未直接展开]"
            )

        try:
            raw = decode_inline_data(data_b64)
        except Exception:
            return f"[上传文件: {display_name}, 内容解码失败]"

        text = extract_inline_attachment_text(
            display_name=display_name,
            mime_type=mime_type,
            raw=raw,
        )
        if text:
            return f"[上传文件: {display_name}]\n{text}"
        return (
            "[上传文件: "
            f"{display_name}, "
            f"mime={mime_type or 'application/octet-stream'}, "
            f"bytes={len(raw)}]"
        )

    storage_path_value = attachment.get("storage_path")
    storage_path = Path(str(storage_path_value)) if storage_path_value else None
    size_bytes = attachment.get("size_bytes")
    if size_bytes is None and storage_path is not None and storage_path.exists():
        try:
            size_bytes = storage_path.stat().st_size
        except OSError:
            size_bytes = None

    raw = read_attachment_bytes(storage_path, size_limit=_MAX_REFERENCE_TEXT_BYTES)
    if raw is not None:
        text = extract_inline_attachment_text(
            display_name=display_name,
            mime_type=mime_type,
            raw=raw,
        )
        if text:
            return f"[上传文件: {display_name}]\n{text}"
        return (
            "[上传文件: "
            f"{display_name}, "
            f"mime={mime_type or 'application/octet-stream'}, "
            f"bytes={len(raw)}]"
        )

    if size_bytes and size_bytes > _MAX_REFERENCE_TEXT_BYTES:
        return (
            "[上传文件: "
            f"{display_name}, "
            f"mime={mime_type or 'unknown'}, "
            f"bytes={size_bytes}, "
            "内容过大，未直接展开]"
        )

    file_uri = attachment.get("file_uri") or ""
    return (
        "[上传文件引用: "
        f"{display_name or file_uri}, "
        f"mime={mime_type or 'unknown'}]"
    )


def extract_user_input_from_parts(parts: List[Part]) -> str:
    segments: List[str] = []

    for part in parts or []:
        if part.text:
            segments.append(part.text)
            continue

        attachment = attachment_from_part(part)
        if attachment:
            segments.append(attachment_prompt_text(attachment))

    return "\n\n".join(s for s in segments if s).strip()


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
    display_content = display_content_from_parts(parts)
    return {
        "content": extract_user_input_from_parts(parts),
        "display_content": display_content,
        "parts": [part.model_dump(exclude_none=True) for part in parts],
        "attachments": attachments,
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
            }
        ]
    if isinstance(input_payload, list):
        return normalize_kop_messages(input_payload)
    return []
