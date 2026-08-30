from __future__ import annotations

from openai_codex import ImageInput, MentionInput, TextInput

from ksadk.codex.runtime import _build_run_input, _materialize_inline_file
from ksadk.runtime.adapter import StartRequest


def test_codex_turn_preserves_text_image_and_inline_file_parts():
    request = StartRequest(
        input=[
            {"type": "input_text", "text": "summarize the attachments"},
            {"type": "input_image", "image_url": "data:image/png;base64,eA=="},
            {
                "type": "input_file",
                "filename": "notes.txt",
                "file_data": "data:text/plain;base64,aGVsbG8=",
            },
        ],
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
    )

    result = _build_run_input(request, request.input)

    assert isinstance(result, list)
    assert isinstance(result[0], TextInput)
    assert result[0].text == "summarize the attachments"
    assert isinstance(result[1], ImageInput)
    assert result[1].url == "data:image/png;base64,eA=="
    attachment_context = next(
        item
        for item in result
        if isinstance(item, TextInput) and "<uploaded_attachment" in item.text
    )
    assert 'name="notes.txt"' in attachment_context.text
    assert "hello" in attachment_context.text
    mention = next(item for item in result if isinstance(item, MentionInput))
    assert mention.name == "notes.txt"
    assert mention.path.endswith("/notes.txt")


def test_inline_file_materialization_is_bounded_and_sanitizes_filename():
    path = _materialize_inline_file(
        "data:text/plain;base64,aGVsbG8=", "../../customer notes.txt"
    )

    assert path is not None
    assert path.name == "customer-notes.txt"
    assert path.read_bytes() == b"hello"
    raw_base64_path = _materialize_inline_file("aGVsbG8=", "raw.txt")
    assert raw_base64_path is not None
    assert raw_base64_path.read_bytes() == b"hello"
    assert _materialize_inline_file("not-a-data-url", "bad.txt") is None
    assert _materialize_inline_file("data:text/plain;base64,%%%", "bad.txt") is None
