"""Resolve bounded inline documents before the original Native start transaction."""

import base64
import binascii
import hashlib
import io

from ksadk.conversations.attachments import build_attachment_result, classify_attachment_kind
from ksadk.harness.native.user_document import NativeUserDocument
from ksadk.studio.attachment_store import ConversationAttachmentStore


def native_document_input(part):
    if set(part) != {"type", "filename", "file_data"}:
        raise ValueError("native_user_document_input_invalid")
    data, name = part["file_data"], part["filename"]
    maximum = ConversationAttachmentStore.MAX_BYTES
    if (
        not isinstance(data, str)
        or len(data) > (maximum + 2) // 3 * 4 + 512
        or not isinstance(name, str)
        or not 1 <= len(name) <= 256
    ):
        raise ValueError("native_user_document_input_invalid")
    header, separator, payload = data.partition(",")
    if not separator or not header.startswith("data:") or not header.endswith(";base64"):
        raise ValueError("native_user_document_source_unsupported")
    mime = header[5:-7].strip().lower()
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("native_user_document_input_invalid") from None
    if not raw or len(raw) > maximum:
        raise ValueError("native_user_document_input_too_large")
    if not ConversationAttachmentStore._supported(raw, filename=name, media_type=mime):
        raise ValueError("native_user_document_type_unsupported")
    kind = classify_attachment_kind(mime, name)
    if kind == "image":
        raise ValueError("native_user_document_type_unsupported")
    result = build_attachment_result(
        {
            "display_name": name,
            "mime_type": mime if kind == "document" else "text/plain",
            "transport": "inline",
            "data": payload,
            "size_bytes": len(raw),
        },
        capture_full_text=True,
    )
    text = result.get("text")
    if result.get("status") != "ok" or not isinstance(text, str) or not text.strip():
        raise ValueError("native_user_document_text_unavailable")
    pages = {}
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        from pypdf import PdfReader

        try:
            total = len(PdfReader(io.BytesIO(raw)).pages)
        except Exception:
            raise ValueError("native_user_document_text_unavailable") from None
        pages = {"total_pages": total}
        if result["extraction_method"] == "pdf_native":
            metadata = result.get("document", {})
            pages.update(
                {
                    key: metadata[key]
                    for key in (
                        "extracted_pages",
                        "failed_page_count",
                        "pages_without_text",
                        "text_extraction_complete",
                    )
                    if key in metadata
                }
            )
    full_text = result.get("full_text")
    return NativeUserDocument(
        name=name,
        media_type=mime,
        source_digest=hashlib.sha256(raw).hexdigest(),
        source_size_bytes=len(raw),
        text=result["text_excerpt"] if full_text is not None else text,
        extraction_method=result["extraction_method"],
        truncated=result.get("text_truncated", False),
        extracted_text_chars=result.get("original_text_chars"),
        full_text=full_text,
        **pages,
    )
