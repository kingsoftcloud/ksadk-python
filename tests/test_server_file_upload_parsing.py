import base64

from ksadk.server.api_models import FileData, InlineData, Part
from ksadk.server.app import _attachment_from_part, _extract_user_input_from_parts


def test_extract_user_input_from_text_part():
    parts = [Part(text="看下这个候选人简历")]
    text = _extract_user_input_from_parts(parts)
    assert text == "看下这个候选人简历"


def test_extract_user_input_from_inline_text_file():
    content = "张三\n8年经验\n熟悉LangGraph"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parts = [
        Part(
            inlineData=InlineData(
                data=encoded,
                mimeType="text/plain",
                displayName="张三.txt",
            )
        )
    ]

    text = _extract_user_input_from_parts(parts)
    assert "[上传文件: 张三.txt]" in text
    assert "8年经验" in text


def test_extract_user_input_from_binary_file_keeps_metadata():
    encoded = base64.b64encode(b"\x89PNG\r\n").decode("ascii")
    parts = [
        Part(
            inlineData=InlineData(
                data=encoded,
                mimeType="image/png",
                displayName="avatar.png",
            )
        )
    ]

    text = _extract_user_input_from_parts(parts)
    assert "avatar.png" in text
    assert "image/png" in text


def test_extract_user_input_from_file_reference():
    parts = [
        Part(
            fileData=FileData(
                fileUri="ks3://bucket/path/a.txt",
                mimeType="text/plain",
                displayName="a.txt",
            )
        )
    ]

    text = _extract_user_input_from_parts(parts)
    assert "上传文件引用" in text
    assert "a.txt" in text


def test_extract_user_input_from_local_file_reference_outside_uploads_dir_keeps_reference_only(tmp_path):
    attachment_path = tmp_path / "resume.txt"
    attachment_path.write_text("张三\n8年经验\n熟悉LangGraph", encoding="utf-8")
    parts = [
        Part(
            fileData=FileData(
                fileUri=f"local:{attachment_path}",
                mimeType="text/plain",
                displayName="resume.txt",
            )
        )
    ]

    text = _extract_user_input_from_parts(parts)
    assert "上传文件引用" in text
    assert "resume.txt" in text
    assert "8年经验" not in text


def test_extract_user_input_from_opaque_upload_handle_reads_text(monkeypatch, tmp_path):
    ui_dir = tmp_path / ".agentengine" / "ui"
    uploads_dir = ui_dir / "files"
    uploads_dir.mkdir(parents=True)
    stored_file = uploads_dir / "abc123.txt"
    stored_file.write_text("候选人简历内容\n熟悉DeepAgents", encoding="utf-8")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    parts = [
        Part(
            fileData=FileData(
                fileUri="ksadk-upload://abc123",
                mimeType="text/plain",
                displayName="resume.txt",
            )
        )
    ]

    text = _extract_user_input_from_parts(parts)
    assert "[上传文件: resume.txt]" in text
    assert "候选人简历内容" in text


def test_attachment_from_part_resolves_storage_path_for_upload_handle(monkeypatch, tmp_path):
    ui_dir = tmp_path / ".agentengine" / "ui"
    uploads_dir = ui_dir / "files"
    uploads_dir.mkdir(parents=True)
    stored_file = uploads_dir / "abc123.txt"
    stored_file.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    attachment = _attachment_from_part(
        Part(
            fileData=FileData(
                fileUri="ksadk-upload://abc123",
                mimeType="text/plain",
                displayName="resume.txt",
            )
        )
    )

    assert attachment is not None
    assert attachment["transport"] == "reference"
    assert attachment["file_uri"] == "ksadk-upload://abc123"
    assert attachment["storage_path"] == str(stored_file)
    assert attachment["size_bytes"] == 5
    assert attachment["is_text"] is True
