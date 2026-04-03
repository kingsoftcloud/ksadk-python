from types import SimpleNamespace

import pytest
from langgraph.types import Command

from ksadk.runners.langgraph_runner import LangGraphRunner


class _DummyAgent:
    def __init__(self):
        self.last_ainvoke_state = None
        self.last_astream_state = None

    async def ainvoke(self, state, config=None):
        self.last_ainvoke_state = state
        return {"messages": [{"content": "ok"}]}

    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        if False:
            yield {}


def _make_runner() -> LangGraphRunner:
    detection = SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent")
    runner = LangGraphRunner(detection, ".")
    runner._agent = _DummyAgent()
    return runner


@pytest.mark.asyncio
async def test_invoke_simplified_input_preserves_extra_state():
    runner = _make_runner()

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "hello",
            "history": [{"role": "user", "content": "prev"}],
            "files": [{"name": "resume.txt"}],
        }
    )

    state = runner._agent.last_ainvoke_state
    assert "messages" in state
    assert "files" in state
    assert state["files"] == [{"name": "resume.txt"}]
    assert len(state["messages"]) == 2


@pytest.mark.asyncio
async def test_invoke_simplified_input_preserves_attachment_contract_fields():
    runner = _make_runner()

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "请分析附件",
            "history": [{"role": "user", "content": "上一轮"}],
            "input_parts": [{"text": "请分析附件"}],
            "attachments": [{"display_name": "resume.pdf"}],
            "attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
        }
    )

    state = runner._agent.last_ainvoke_state
    assert state["input_parts"] == [{"text": "请分析附件"}]
    assert state["attachments"] == [{"display_name": "resume.pdf"}]
    assert state["attachment_results"] == [{"display_name": "resume.pdf", "kind": "document"}]
    assert len(state["messages"]) == 2


@pytest.mark.asyncio
async def test_stream_resume_uses_command():
    runner = _make_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "s1",
                "resume": True,
                "input": {"approved": True},
            }
        )
    ]

    assert isinstance(runner._agent.last_astream_state, Command)
    assert runner._agent.last_astream_state.resume == {"approved": True}
    assert isinstance(runner._agent.last_ainvoke_state, Command)
    assert runner._agent.last_ainvoke_state.resume == {"approved": True}
    assert chunks and chunks[-1]["type"] == "final"


@pytest.mark.asyncio
async def test_invoke_with_binary_attachment_does_not_convert_reference_to_image_url():
    runner = _make_runner()

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "分析压缩包",
            "attachments": [
                {
                    "display_name": "bundle.zip",
                    "mime_type": "application/zip",
                    "transport": "reference",
                    "file_uri": "ksadk-upload://abc123",
                    "storage_path": "/tmp/abc123.zip",
                }
            ],
        }
    )

    content = runner._agent.last_ainvoke_state["messages"][-1].content
    if isinstance(content, list):
        assert not any(item.get("type") == "image_url" for item in content if isinstance(item, dict))
    else:
        assert content == "分析压缩包"
