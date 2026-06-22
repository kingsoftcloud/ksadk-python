from __future__ import annotations

import base64
import io
import json
import mimetypes
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional
from xml.etree import ElementTree as ET

from ksadk.conversations.attachment_storage import (
    AttachmentStorageService,
    is_hosted_upload_uri,
    is_runtime_upload_uri,
    parse_file_id,
)
from ksadk.sessions.local_service import resolve_local_session_dir

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
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
}
_DOCUMENT_FILE_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".html",
    ".htm",
}
_IMAGE_FILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_ARCHIVE_FILE_EXTENSIONS = {".zip"}
_DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/html",
}
_ARCHIVE_MIME_TYPES = {"application/zip", "application/x-zip-compressed"}
_IMAGE_MIME_PREFIX = "image/"
_UPLOAD_URI_SCHEME = "ksadk-upload://"
_MAX_INLINE_TEXT_CHARS = 20_000
_MAX_TEXT_EXCERPT_CHARS = 600
_MAX_PROCESS_BYTES = 20_000_000
_MAX_ARCHIVE_ENTRY_BYTES = 3_000_000
_MAX_ARCHIVE_TOTAL_BYTES = 12_000_000
_MAX_ARCHIVE_ENTRIES = 50
_NESTED_ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
_BLOCKED_EXECUTABLE_EXTENSIONS = {
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
    ".bash",
    ".zsh",
    ".app",
    ".msi",
    ".jar",
    ".com",
    ".scr",
}


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


def _path_within_root(path: Path, root: Path) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    resolved_root = root.expanduser().resolve(strict=False)
    return resolved == resolved_root or resolved_root in resolved.parents


def resolve_attachment_storage_path(file_uri: str) -> Optional[Path]:
    normalized_uri = (file_uri or "").strip()
    if not normalized_uri:
        return None

    if normalized_uri.startswith("local:"):
        path = Path(normalized_uri[6:]).expanduser()
        resolved = path.resolve()
        uploads_dir = resolve_uploads_dir().resolve()
        try:
            resolved.relative_to(uploads_dir)
        except ValueError:
            return None
        return resolved

    if is_runtime_upload_uri(normalized_uri) or is_hosted_upload_uri(normalized_uri):
        file_id = parse_file_id(normalized_uri)
        if not file_id:
            return None

        uploads_dir = resolve_uploads_dir().resolve()
        restored = AttachmentStorageService().ensure_local_path(normalized_uri)
        if restored is not None:
            resolved = restored.resolve(strict=False)
            if _path_within_root(resolved, uploads_dir) and resolved.is_file():
                return resolved

        safe_file_id = Path(file_id).name
        if not safe_file_id:
            return None
        for candidate in sorted(uploads_dir.glob(f"{safe_file_id}*")):
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


def read_resolved_attachment_bytes(
    storage_path: Any,
    *,
    size_limit: Optional[int] = None,
) -> Optional[bytes]:
    if storage_path is None:
        return None
    return read_attachment_bytes(Path(str(storage_path)), size_limit=size_limit)


def classify_attachment_kind(mime_type: str, display_name: str) -> str:
    mime = (mime_type or "").lower()
    suffix = Path(display_name or "").suffix.lower()
    if suffix in _IMAGE_FILE_EXTENSIONS or mime.startswith(_IMAGE_MIME_PREFIX):
        return "image"
    if suffix in _ARCHIVE_FILE_EXTENSIONS or mime in _ARCHIVE_MIME_TYPES:
        return "archive"
    if suffix in _DOCUMENT_FILE_EXTENSIONS or mime in _DOCUMENT_MIME_TYPES:
        return "document"
    if suffix in _TEXT_FILE_EXTENSIONS or looks_like_textual_attachment(mime, display_name):
        return "text"
    return "binary"


def perform_ocr(raw: bytes, mime_type: str, display_name: str) -> dict[str, str]:
    rapid_text = _perform_rapidocr(raw)
    if rapid_text:
        return {"text": rapid_text, "engine": "rapidocr_onnxruntime"}

    tesseract_text = _perform_tesseract_ocr(raw)
    if tesseract_text:
        return {"text": tesseract_text, "engine": "pytesseract"}

    return {"text": "", "engine": ""}


def build_attachment_results(attachments: List[Dict[str, Any]]) -> list[dict[str, Any]]:
    return [build_attachment_result(attachment) for attachment in attachments or []]


def build_attachment_result(attachment: Mapping[str, Any]) -> dict[str, Any]:
    display_name = str(attachment.get("display_name") or "uploaded_file")
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    transport = str(attachment.get("transport") or "")
    file_uri = str(attachment.get("file_uri") or "")
    size_bytes = attachment.get("size_bytes")
    raw = _load_attachment_bytes(attachment)
    if size_bytes is None and raw is not None:
        size_bytes = len(raw)

    return _build_attachment_result_from_raw(
        display_name=display_name,
        mime_type=mime_type,
        transport=transport,
        file_uri=file_uri,
        size_bytes=size_bytes,
        raw=raw,
        allow_archive=True,
    )


def build_attachment_prompt_text(result: Mapping[str, Any]) -> str:
    display_name = str(result.get("display_name") or "uploaded_file")
    mime_type = str(result.get("mime_type") or "application/octet-stream")
    transport = str(result.get("transport") or "")
    text = str(result.get("text") or "").strip()
    if text:
        return f"[上传文件: {display_name}]\n{text}"

    archive = result.get("archive")
    if isinstance(archive, Mapping):
        entries = archive.get("entries") or []
        entry_lines = [
            f"- {entry.get('path')}"
            for entry in entries[:10]
            if isinstance(entry, Mapping) and str(entry.get("path") or "").strip()
        ]
        if entry_lines:
            return f"[上传文件: {display_name}]\n压缩包内容：\n" + "\n".join(entry_lines)

    warnings = [str(item).strip() for item in result.get("warnings") or [] if str(item).strip()]
    if warnings:
        header = (
            f"[上传文件引用: {display_name}, mime={mime_type}]"
            if transport == "reference"
            else f"[上传文件: {display_name}, mime={mime_type}]"
        )
        return header + "\n" + "\n".join(f"- {warning}" for warning in warnings[:5])

    size_bytes = result.get("size_bytes")
    if size_bytes is not None:
        return (
            "[上传文件: "
            f"{display_name}, "
            f"mime={mime_type}, "
            f"bytes={size_bytes}]"
        )

    return f"[上传文件引用: {display_name}, mime={mime_type}]"


def compact_attachment_result_for_session(result: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in (
        "display_name",
        "mime_type",
        "transport",
        "file_uri",
        "size_bytes",
        "kind",
        "status",
        "warnings",
        "extraction_method",
        "text_excerpt",
    ):
        value = result.get(key)
        if value not in (None, ""):
            compact[key] = value

    document = result.get("document")
    if isinstance(document, Mapping):
        compact["document"] = {
            key: value
            for key, value in document.items()
            if key in {"format", "page_count", "ocr_engine"} and value not in (None, "")
        }

    image = result.get("image")
    if isinstance(image, Mapping):
        compact["image"] = {
            key: value
            for key, value in image.items()
            if key in {"ocr_engine"} and value not in (None, "")
        }

    archive = result.get("archive")
    if isinstance(archive, Mapping):
        compact["archive"] = {
            "entries": list(archive.get("entries") or [])[:10],
            "blocked_entries": list(archive.get("blocked_entries") or [])[:10],
            "extracted_entries": list(archive.get("extracted_entries") or [])[:5],
        }

    return compact


def _build_attachment_result_from_raw(
    *,
    display_name: str,
    mime_type: str,
    transport: str,
    file_uri: str,
    size_bytes: Any,
    raw: bytes | None,
    allow_archive: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "display_name": display_name,
        "mime_type": mime_type,
        "transport": transport,
        "file_uri": file_uri,
        "size_bytes": size_bytes,
        "kind": classify_attachment_kind(mime_type, display_name),
        "status": "ok",
        "warnings": [],
        "extraction_method": "metadata_only",
        "text_excerpt": "",
    }

    if raw is None:
        result["status"] = "partial" if size_bytes else "failed"
        result["warnings"] = [
            "附件原始内容不可用，当前仅保留元数据摘要。"
            if size_bytes
            else "附件内容无法读取，请重新上传或检查文件句柄是否仍可访问。"
        ]
        return result

    if result["kind"] == "text":
        return _extract_text_attachment(result, raw)
    if result["kind"] == "document":
        return _extract_document_attachment(result, raw)
    if result["kind"] == "image":
        return _extract_image_attachment(result, raw)
    if result["kind"] == "archive" and allow_archive:
        return _extract_archive_attachment(result, raw)

    result["status"] = "partial"
    result["warnings"] = ["当前附件类型暂不支持结构化解析，已保留基础元数据。"]
    return result


def _extract_text_attachment(result: dict[str, Any], raw: bytes) -> dict[str, Any]:
    text = _decode_text_bytes(raw)
    if not text:
        result["status"] = "failed"
        result["warnings"] = ["文本附件解码失败，请确认文件编码或重新导出后重试。"]
        result["extraction_method"] = "text_decode"
        return result

    return _with_text(result, text=text, extraction_method="text_decode")


def _extract_document_attachment(result: dict[str, Any], raw: bytes) -> dict[str, Any]:
    suffix = Path(str(result.get("display_name") or "")).suffix.lower()
    mime_type = str(result.get("mime_type") or "")

    if suffix == ".pdf" or mime_type == "application/pdf":
        native_text = extract_pdf_text(raw)
        if native_text and not _pdf_text_quality_is_poor(native_text):
            result["document"] = {"format": "pdf"}
            return _with_text(result, text=native_text, extraction_method="pdf_native")

        ocr = perform_ocr(raw, mime_type, str(result.get("display_name") or ""))
        if ocr.get("text"):
            result["document"] = {"format": "pdf", "ocr_engine": ocr.get("engine") or ""}
            result["warnings"] = ["PDF 原生文本抽取为空或质量较差，已回退到 OCR。"]
            return _with_text(result, text=ocr["text"], extraction_method="pdf_ocr")

        result["status"] = "failed"
        result["document"] = {"format": "pdf"}
        result["warnings"] = ["PDF 文本抽取失败，请优先上传可复制文本的 PDF 或更清晰的扫描件。"]
        result["extraction_method"] = "pdf_failed"
        return result

    extractors = {
        ".docx": (_extract_docx_text, "docx_native", {"format": "docx"}),
        ".pptx": (_extract_pptx_text, "pptx_native", {"format": "pptx"}),
        ".xlsx": (_extract_xlsx_text, "xlsx_native", {"format": "xlsx"}),
        ".html": (_extract_html_text, "html_native", {"format": "html"}),
        ".htm": (_extract_html_text, "html_native", {"format": "html"}),
    }
    extractor = extractors.get(suffix)
    if extractor:
        text = extractor[0](raw)
        result["document"] = dict(extractor[2])
        if text:
            return _with_text(result, text=text, extraction_method=extractor[1])
        result["status"] = "failed"
        result["warnings"] = [f"{suffix.lstrip('.').upper()} 文档抽取失败，请检查文件是否损坏或重新导出后重试。"]
        result["extraction_method"] = extractor[1]
        return result

    if suffix in _TEXT_FILE_EXTENSIONS:
        result["document"] = {"format": suffix.lstrip(".")}
        return _extract_text_attachment(result, raw)

    result["status"] = "partial"
    result["document"] = {"format": suffix.lstrip(".") or "document"}
    result["warnings"] = ["当前文档类型暂不支持结构化解析，已保留基础元数据。"]
    return result


def _extract_image_attachment(result: dict[str, Any], raw: bytes) -> dict[str, Any]:
    ocr = perform_ocr(raw, str(result.get("mime_type") or ""), str(result.get("display_name") or ""))
    result["image"] = {"ocr_engine": ocr.get("engine") or ""}
    if ocr.get("text"):
        return _with_text(result, text=ocr["text"], extraction_method="image_ocr")

    result["status"] = "failed"
    result["warnings"] = ["图片 OCR 未产出可用文本，请上传更清晰的图片或改为 PDF/文本版文档。"]
    result["extraction_method"] = "image_ocr"
    return result


def _extract_archive_attachment(result: dict[str, Any], raw: bytes) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    blocked_entries: list[dict[str, Any]] = []
    extracted_entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    extracted_chunks: list[str] = []
    total_safe_bytes = 0

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        result["status"] = "failed"
        result["warnings"] = ["ZIP 压缩包无法打开，请确认文件未损坏后重试。"]
        result["extraction_method"] = "zip_enumeration"
        return result

    with archive:
        infos = archive.infolist()[:_MAX_ARCHIVE_ENTRIES]
        for info in infos:
            normalized_path = _normalize_archive_path(info.filename)
            if not normalized_path:
                warning = f"压缩包内存在危险路径 `{info.filename}`，已阻止提取。"
                warnings.append(warning)
                blocked_entries.append({"path": info.filename, "reason": "path_traversal"})
                continue

            entry = {"path": normalized_path, "size_bytes": info.file_size}
            entries.append(entry)

            suffix = Path(normalized_path).suffix.lower()
            if suffix in _NESTED_ARCHIVE_EXTENSIONS:
                warnings.append(f"压缩包内嵌套压缩文件 `{normalized_path}` 不支持处理，已跳过。")
                blocked_entries.append({**entry, "reason": "nested_archive"})
                continue
            if suffix in _BLOCKED_EXECUTABLE_EXTENSIONS:
                warnings.append(f"压缩包内可执行内容 `{normalized_path}` 已被安全策略阻止。")
                blocked_entries.append({**entry, "reason": "executable"})
                continue
            if info.file_size > _MAX_ARCHIVE_ENTRY_BYTES:
                warnings.append(f"压缩包条目 `{normalized_path}` 超出大小限制，已跳过。")
                blocked_entries.append({**entry, "reason": "entry_too_large"})
                continue
            if total_safe_bytes + info.file_size > _MAX_ARCHIVE_TOTAL_BYTES:
                warnings.append("压缩包可安全展开体积超限，剩余文件已停止提取。")
                blocked_entries.append({**entry, "reason": "total_size_exceeded"})
                continue

            entry_mime = mimetypes.guess_type(normalized_path)[0] or "application/octet-stream"
            entry_kind = classify_attachment_kind(entry_mime, normalized_path)
            if entry_kind not in {"text", "document", "image"}:
                continue

            try:
                entry_raw = archive.read(info)
            except Exception:
                warnings.append(f"压缩包条目 `{normalized_path}` 读取失败，已跳过。")
                blocked_entries.append({**entry, "reason": "read_failed"})
                continue

            total_safe_bytes += len(entry_raw)
            child_result = _build_attachment_result_from_raw(
                display_name=normalized_path,
                mime_type=entry_mime,
                transport="archive_entry",
                file_uri="",
                size_bytes=len(entry_raw),
                raw=entry_raw,
                allow_archive=False,
            )
            extracted_entries.append(
                {
                    "display_name": normalized_path,
                    "kind": child_result.get("kind"),
                    "status": child_result.get("status"),
                    "text_excerpt": child_result.get("text_excerpt", ""),
                }
            )
            if child_result.get("text"):
                extracted_chunks.append(
                    f"[{normalized_path}]\n{child_result['text']}"
                )

    result["archive"] = {
        "entries": entries,
        "blocked_entries": blocked_entries,
        "extracted_entries": extracted_entries,
    }
    result["warnings"] = warnings
    result["extraction_method"] = "zip_enumeration"
    if extracted_chunks:
        result["status"] = "partial" if warnings else "ok"
        return _with_text(
            result,
            text="\n\n".join(extracted_chunks),
            extraction_method="zip_enumeration",
            status="partial" if warnings else "ok",
        )

    result["status"] = "partial" if warnings else "ok"
    result["text_excerpt"] = _truncate_text(
        "\n".join(f"- {entry['path']}" for entry in entries[:10]),
        _MAX_TEXT_EXCERPT_CHARS,
    )
    return result


def _with_text(
    result: dict[str, Any],
    *,
    text: str,
    extraction_method: str,
    status: str = "ok",
) -> dict[str, Any]:
    normalized = _normalize_text(text)
    if not normalized:
        return result
    result["status"] = status
    result["extraction_method"] = extraction_method
    result["text"] = _truncate_text(normalized, _MAX_INLINE_TEXT_CHARS)
    result["text_excerpt"] = _truncate_text(normalized, _MAX_TEXT_EXCERPT_CHARS)
    return result


def _load_attachment_bytes(attachment: Mapping[str, Any]) -> Optional[bytes]:
    inline_data = attachment.get("data")
    if attachment.get("transport") == "inline" and inline_data:
        try:
            raw = decode_inline_data(str(inline_data))
        except Exception:
            return None
        return raw if len(raw) <= _MAX_PROCESS_BYTES else None

    storage_path_value = attachment.get("storage_path")
    raw = read_resolved_attachment_bytes(storage_path_value, size_limit=_MAX_PROCESS_BYTES)
    if raw is not None:
        return raw
    return None


def _decode_text_bytes(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            text = raw.decode(encoding)
        except Exception:
            continue
        if text:
            return text
    return raw.decode("utf-8", errors="ignore")


def _extract_docx_text(raw: bytes) -> str:
    return _extract_xml_text_from_zip(raw, prefixes=("word/document", "word/header", "word/footer"))


def _extract_pptx_text(raw: bytes) -> str:
    return _extract_xml_text_from_zip(raw, prefixes=("ppt/slides/slide",))


def _extract_xlsx_text(raw: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        return ""

    with archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for node in root.iter():
                    if node.tag.endswith("}t") and node.text:
                        shared_strings.append(node.text)
            except Exception:
                shared_strings = []

        rows: list[str] = []
        for name in sorted(item for item in archive.namelist() if item.startswith("xl/worksheets/") and item.endswith(".xml")):
            try:
                root = ET.fromstring(archive.read(name))
            except Exception:
                continue
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                values: list[str] = []
                for cell in row:
                    if not isinstance(cell.tag, str) or not cell.tag.endswith("}c"):
                        continue
                    cell_type = cell.attrib.get("t")
                    value_text = ""
                    if cell_type == "s":
                        value = next((child.text for child in cell if child.tag.endswith("}v")), "")
                        if value and value.isdigit():
                            index = int(value)
                            if 0 <= index < len(shared_strings):
                                value_text = shared_strings[index]
                    elif cell_type == "inlineStr":
                        value_text = " ".join(
                            child.text or ""
                            for child in cell.iter()
                            if isinstance(child.tag, str) and child.tag.endswith("}t")
                        )
                    else:
                        value_text = next((child.text or "" for child in cell if child.tag.endswith("}v")), "")
                    if value_text.strip():
                        values.append(value_text.strip())
                if values:
                    rows.append("\t".join(values))
        return "\n".join(rows)


def _extract_html_text(raw: bytes) -> str:
    text = _decode_text_bytes(raw)
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return text

    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception:
        return text
    return soup.get_text("\n", strip=True)


def _extract_xml_text_from_zip(raw: bytes, *, prefixes: tuple[str, ...]) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        return ""

    texts: list[str] = []
    with archive:
        for name in sorted(
            item for item in archive.namelist() if item.endswith(".xml") and item.startswith(prefixes)
        ):
            try:
                root = ET.fromstring(archive.read(name))
            except Exception:
                continue
            fragment = [
                node.text.strip()
                for node in root.iter()
                if isinstance(node.tag, str) and node.tag.endswith("}t") and (node.text or "").strip()
            ]
            if fragment:
                texts.append("\n".join(fragment))
    return "\n\n".join(texts)


def _normalize_text(text: str) -> str:
    return str(text or "").strip()


def _truncate_text(text: str, limit: int) -> str:
    normalized = _normalize_text(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "\n...[内容已截断]"


def _pdf_text_quality_is_poor(text: str) -> bool:
    normalized = "".join(ch for ch in text if not ch.isspace())
    if len(normalized) < 20:
        return True
    readable = sum(
        1
        for ch in normalized
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff")
    )
    return readable / max(len(normalized), 1) < 0.5


def _perform_rapidocr(raw: bytes) -> str:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return ""

    try:
        engine = RapidOCR()
        result, _ = engine(raw)
    except Exception:
        return ""

    texts = [
        str(item[1]).strip()
        for item in result or []
        if isinstance(item, (list, tuple)) and len(item) > 1 and str(item[1]).strip()
    ]
    return "\n".join(texts).strip()


def _perform_tesseract_ocr(raw: bytes) -> str:
    try:
        from PIL import Image
        import pytesseract
    except Exception:
        return ""

    try:
        image = Image.open(io.BytesIO(raw))
    except Exception:
        return ""

    for lang in ("chi_sim+eng", "chi_sim", "eng"):
        try:
            text = pytesseract.image_to_string(image, lang=lang)
        except Exception:
            continue
        if text and text.strip():
            return text.strip()
    return ""


def _normalize_archive_path(path: str) -> str:
    raw_path = (path or "").replace("\\", "/").strip()
    if not raw_path:
        return ""
    pure_path = PurePosixPath(raw_path)
    parts = pure_path.parts
    if any(part in {"..", ""} for part in parts):
        return ""
    normalized = str(pure_path).lstrip("./")
    if not normalized or normalized.startswith("/") or normalized == ".":
        return ""
    return normalized
