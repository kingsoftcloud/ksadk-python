import base64

from ksadk.server.api_models import FileData, InlineData, Part
from ksadk.server.app import _extract_user_input_from_parts


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
