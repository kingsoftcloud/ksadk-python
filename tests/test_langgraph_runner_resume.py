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
