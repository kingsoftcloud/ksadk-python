from __future__ import annotations

import base64
import io
import zipfile

import pytest

from ksadk.conversations.normalize import normalize_parts_content
from ksadk.server.api_models import InlineData, Part


def _inline_part(*, name: str, mime_type: str, raw: bytes) -> Part:
    return Part(
        inlineData=InlineData(
            data=base64.b64encode(raw).decode("ascii"),
            mimeType=mime_type,
            displayName=name,
        )
    )


def test_normalize_parts_content_returns_attachment_results_for_inline_text_file():
    payload = normalize_parts_content(
        [_inline_part(name="resume.txt", mime_type="text/plain", raw="张三\n8年经验".encode("utf-8"))]
    )

    assert payload["attachments"][0]["display_name"] == "resume.txt"
    result = payload["attachment_results"][0]
    assert result["display_name"] == "resume.txt"
    assert result["kind"] == "text"
    assert result["status"] == "ok"
    assert result["extraction_method"] == "text_decode"
    assert result["text_excerpt"] == "张三\n8年经验"
    assert result["text"] == "张三\n8年经验"
    assert payload["content"].startswith("[上传文件: resume.txt]")
    assert "8年经验" in payload["content"]


def test_normalize_parts_content_falls_back_to_pdf_ocr_when_native_extract_is_empty(monkeypatch):
    monkeypatch.setattr(
        "ksadk.conversations.attachments.extract_pdf_text",
        lambda raw: "",
    )
    monkeypatch.setattr(
        "ksadk.conversations.attachments.perform_ocr",
        lambda raw, mime_type, display_name: {
            "text": "李四 10年产品经验",
            "engine": "mock-ocr",
        },
    )

    payload = normalize_parts_content(
        [_inline_part(name="resume.pdf", mime_type="application/pdf", raw=b"%PDF-1.4 fake")]
    )

    result = payload["attachment_results"][0]
    assert result["kind"] == "document"
    assert result["status"] == "ok"
    assert result["extraction_method"] == "pdf_ocr"
    assert result["text"] == "李四 10年产品经验"
    assert any("OCR" in warning for warning in result["warnings"])
    assert result["document"]["ocr_engine"] == "mock-ocr"


def test_normalize_parts_content_uses_ocr_for_image_attachments(monkeypatch):
    monkeypatch.setattr(
        "ksadk.conversations.attachments.perform_ocr",
        lambda raw, mime_type, display_name: {
            "text": "王五\n算法工程师",
            "engine": "mock-ocr",
        },
    )

    payload = normalize_parts_content(
        [_inline_part(name="avatar.png", mime_type="image/png", raw=b"\x89PNG\r\n")]
    )

    result = payload["attachment_results"][0]
    assert result["kind"] == "image"
    assert result["status"] == "ok"
    assert result["extraction_method"] == "image_ocr"
    assert result["text"] == "王五\n算法工程师"
    assert result["image"]["ocr_engine"] == "mock-ocr"


def test_normalize_parts_content_safely_enumerates_zip_and_blocks_nested_archives():
    archive_stream = io.BytesIO()
    with zipfile.ZipFile(archive_stream, "w") as archive:
        archive.writestr("resume.txt", "候选人A\n负责增长业务")
        archive.writestr("nested.zip", b"PK\x03\x04not-allowed")
        archive.writestr("../escape.txt", "blocked")

    payload = normalize_parts_content(
        [_inline_part(name="bundle.zip", mime_type="application/zip", raw=archive_stream.getvalue())]
    )

    result = payload["attachment_results"][0]
    assert result["kind"] == "archive"
    assert result["status"] == "partial"
    assert result["extraction_method"] == "zip_enumeration"
    assert any("nested.zip" in warning for warning in result["warnings"])
    assert any("escape.txt" in warning for warning in result["warnings"])
    assert result["archive"]["entries"][0]["path"] == "resume.txt"
    assert result["archive"]["extracted_entries"][0]["display_name"] == "resume.txt"
    assert "候选人A" in result["text"]
