from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.history import RunnableWithMessageHistory

from ksadk.runners.langchain_runner import LangChainRunner


class _RecordingAgent:
    def __init__(self):
        self.calls: list[tuple[dict, dict | None]] = []

    async def ainvoke(self, payload, config=None):
        self.calls.append((payload, config))
        return {"output": "ok"}


class _UsageMessage:
    def __init__(self):
        self.content = "ok"
        self.usage_metadata = {
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
            "input_token_details": {},
            "output_token_details": {"reasoning": 3},
        }


class _UsageAgent:
    async def ainvoke(self, payload, config=None):
        del payload, config
        return {"messages": [_UsageMessage()]}


class _UsageStreamingAgent:
    async def astream(self, payload, config=None):
        del payload, config
        yield _UsageMessage()


def _make_runner(agent, module=None) -> LangChainRunner:
    detection = SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent")
    runner = LangChainRunner(detection, ".")
    runner._agent = agent
    runner._module = module or SimpleNamespace()
    return runner


@pytest.mark.asyncio
async def test_langchain_runner_uses_standard_prepare_input_hook():
    agent = _RecordingAgent()
    captured: list[tuple[dict, dict]] = []

    def ksadk_prepare_input(payload: dict, session_context: dict) -> dict:
        captured.append((payload, session_context))
        return {
            "question": payload["input"],
            "history_len": len(session_context["history"]),
            "session_id": session_context["session_id"],
        }

    runner = _make_runner(agent, module=SimpleNamespace(ksadk_prepare_input=ksadk_prepare_input))

    result = await runner.invoke(
        {
            "session_id": "sess-1",
            "input": "现在进展到哪了",
            "history": [
                {"role": "user", "content": "我叫张三"},
                {"role": "model", "content": "记住了"},
            ],
        }
    )

    assert result["output"] == "ok"
    assert captured == [
        (
            {"input": "现在进展到哪了"},
            {
                "session_id": "sess-1",
                "history": [
                    {"role": "user", "content": "我叫张三"},
                    {"role": "model", "content": "记住了"},
                ],
                "input_parts": [],
                "attachments": [],
                "attachment_results": [],
                "instructions": None,
                "platform_context": None,
                "kb_context": None,
                "memory_context": None,
            },
        )
    ]
    assert agent.calls[0][0] == {
        "question": "现在进展到哪了",
        "history_len": 2,
        "session_id": "sess-1",
    }


@pytest.mark.asyncio
async def test_langchain_runner_uses_runnable_with_message_history_session_config():
    store: dict[str, InMemoryChatMessageHistory] = {}

    def get_history(session_id: str) -> InMemoryChatMessageHistory:
        return store.setdefault(session_id, InMemoryChatMessageHistory())

    def sync_chain(payload: dict) -> dict:
        messages = payload["input"]
        return {"output": f"history={len(messages)}"}

    runnable = RunnableWithMessageHistory(RunnableLambda(sync_chain), get_history)
    runner = _make_runner(runnable)

    result = await runner.invoke({"session_id": "sess-history", "input": "hello"})

    assert result["output"] == "history=1"
    assert [message.content for message in store["sess-history"].messages] == ["hello", "history=1"]


@pytest.mark.asyncio
async def test_langchain_runner_falls_back_to_transcript_replay_prompt():
    agent = _RecordingAgent()
    runner = _make_runner(agent)

    await runner.invoke(
        {
            "session_id": "sess-replay",
            "input": "那我叫什么",
            "history": [
                {"role": "user", "content": "我叫张三"},
                {"role": "model", "content": "我记住了"},
                {"role": "user", "content": "那我叫什么"},
            ],
        }
    )

    payload, _config = agent.calls[0]
    assert payload["input"].startswith("Conversation history:")
    assert "user: 我叫张三" in payload["input"]
    assert "assistant: 我记住了" in payload["input"]
    assert payload["input"].rstrip().endswith("user: 那我叫什么")


@pytest.mark.asyncio
async def test_langchain_runner_standard_hook_receives_platform_kb_and_memory_context():
    agent = _RecordingAgent()
    captured: list[dict] = []

    def ksadk_prepare_input(payload: dict, session_context: dict) -> dict:
        captured.append(session_context)
        return payload

    runner = _make_runner(agent, module=SimpleNamespace(ksadk_prepare_input=ksadk_prepare_input))

    await runner.invoke(
        {
            "session_id": "sess-2",
            "input": "查一下最新支持库",
            "platform_context": {"agent_id": "demo-agent", "user_id": "user-1"},
            "kb_context": {"formatted_text": "KB facts"},
            "memory_context": {"formatted_text": "Memory facts"},
        }
    )

    assert captured == [
        {
            "session_id": "sess-2",
            "history": [],
            "input_parts": [],
            "attachments": [],
            "attachment_results": [],
            "instructions": None,
            "platform_context": {"agent_id": "demo-agent", "user_id": "user-1"},
            "kb_context": {"formatted_text": "KB facts"},
            "memory_context": {"formatted_text": "Memory facts"},
        }
    ]


@pytest.mark.asyncio
async def test_langchain_runner_replay_prompt_includes_ambient_kb_and_memory_context():
    agent = _RecordingAgent()
    runner = _make_runner(agent)

    await runner.invoke(
        {
            "session_id": "sess-3",
            "input": "继续",
            "kb_context": {"formatted_text": "知识库: 当前支持标准型和计算型"},
            "memory_context": {"formatted_text": "记忆: 用户上次查过主机机型"},
        }
    )

    payload, _config = agent.calls[0]
    assert "Knowledge base context:" in payload["input"]
    assert "知识库: 当前支持标准型和计算型" in payload["input"]
    assert "Long-term memory context:" in payload["input"]
    assert "记忆: 用户上次查过主机机型" in payload["input"]


@pytest.mark.asyncio
async def test_langchain_runner_replay_prompt_includes_instructions():
    agent = _RecordingAgent()
    runner = _make_runner(agent)

    await runner.invoke(
        {
            "session_id": "sess-instructions",
            "input": "hello",
            "instructions": "只用中文回答",
        }
    )

    payload, _config = agent.calls[0]
    assert payload["input"].startswith("只用中文回答")
    assert payload["input"].rstrip().endswith("user: hello")


@pytest.mark.asyncio
async def test_langchain_runner_message_history_includes_instructions_without_ambient_context():
    store: dict[str, InMemoryChatMessageHistory] = {}
    seen_messages = []

    def get_history(session_id: str) -> InMemoryChatMessageHistory:
        return store.setdefault(session_id, InMemoryChatMessageHistory())

    def sync_chain(payload: dict) -> dict:
        seen_messages.append(payload["input"])
        return {"output": "ok"}

    runnable = RunnableWithMessageHistory(RunnableLambda(sync_chain), get_history)
    runner = _make_runner(runnable)

    result = await runner.invoke(
        {
            "session_id": "sess-history-instructions",
            "input": "hello",
            "instructions": "只用中文回答",
        }
    )

    assert result["output"] == "ok"
    assert seen_messages
    assert seen_messages[0][0].__class__.__name__ == "SystemMessage"
    assert "只用中文回答" in seen_messages[0][0].content
    assert seen_messages[0][1].content == "hello"


@pytest.mark.asyncio
async def test_langchain_runner_invoke_extracts_usage_from_message_metadata():
    runner = _make_runner(_UsageAgent())

    result = await runner.invoke({"session_id": "sess-usage", "input": "hello"})

    assert result["usage"] == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "input_token_details": {},
        "output_token_details": {"reasoning": 3},
    }


@pytest.mark.asyncio
async def test_langchain_runner_stream_emits_final_usage_from_last_chunk():
    runner = _make_runner(_UsageStreamingAgent())

    chunks = [chunk async for chunk in runner.stream({"session_id": "sess-usage", "input": "hello"})]

    assert chunks == [
        {"delta": "ok", "type": "text"},
        {
            "output": "ok",
            "type": "final",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "input_token_details": {},
                "output_token_details": {"reasoning": 3},
            },
        },
    ]


def test_langchain_runner_extracts_wrapped_history_runnable():
    store: dict[str, InMemoryChatMessageHistory] = {}

    def get_history(session_id: str) -> InMemoryChatMessageHistory:
        return store.setdefault(session_id, InMemoryChatMessageHistory())

    runnable = RunnableLambda(lambda payload: {"output": payload["input"]})
    wrapped = RunnableWithMessageHistory(runnable, get_history)
    runner = _make_runner(wrapped)

    extracted = runner._extract_wrapped_history_runnable()

    assert extracted is not None
    assert hasattr(extracted, "invoke")


def test_langchain_runner_logs_unknown_wrapped_history_shape(caplog):
    caplog.set_level("DEBUG", logger="ksadk.runners.langchain_runner")
    runner = _make_runner(SimpleNamespace(bound=object()))

    assert runner._extract_wrapped_history_runnable() is None
    assert "Unable to inspect RunnableWithMessageHistory wrapper" in caplog.text
