from types import SimpleNamespace

import pytest
from langgraph.types import Command
import base64

from ksadk.runners.langgraph_runner import LangGraphRunner


class _DummyAgent:
    def __init__(self):
        self.last_ainvoke_state = None
        self.last_astream_state = None
        self.last_ainvoke_context = None
        self.last_ainvoke_config = None
        self.last_astream_config = None
        self.state_config = None

    async def ainvoke(self, state, config=None, context=None):
        self.last_ainvoke_state = state
        self.last_ainvoke_context = context
        self.last_ainvoke_config = config
        return {"messages": [{"content": "ok"}]}

    def get_state(self, config):
        del config
        return SimpleNamespace(config=self.state_config)

    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        self.last_astream_config = config
        if False:
            yield {}


class _AsyncStateAgent(_DummyAgent):
    async def aget_state(self, config):
        del config
        return SimpleNamespace(config=self.state_config)

    get_state = None


class _Chunk:
    def __init__(self, content="", reasoning_content=None):
        self.content = content
        self.additional_kwargs = {}
        if reasoning_content is not None:
            self.additional_kwargs["reasoning_content"] = reasoning_content


class _StreamingAgent(_DummyAgent):
    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": _Chunk(reasoning_content="先分析需求。")},
        }
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": _Chunk(content="这是最终回复。")},
        }


class _DuplicatedReasoningStreamingAgent(_DummyAgent):
    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        yield {
            "event": "on_chat_model_stream",
            "data": {
                "chunk": _Chunk(
                    content="先分析需求。",
                    reasoning_content="先分析需求。",
                )
            },
        }
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": _Chunk(content="这是最终回复。")},
        }


class _ToolDictOutputStreamingAgent(_DummyAgent):
    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        yield {
            "event": "on_tool_end",
            "name": "write_workspace_file",
            "run_id": "run-approval",
            "data": {
                "output": {
                    "ok": False,
                    "type": "approval_required",
                    "approval_request": {
                        "id": "appr_write",
                        "tool_name": "write_workspace_file",
                    },
                }
            },
        }


class _ToolThenAnswerStreamingAgent(_DummyAgent):
    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        yield {
            "event": "on_tool_start",
            "name": "list_skills",
            "run_id": "run-list-skills",
            "data": {"input": {}},
        }
        yield {
            "event": "on_tool_end",
            "name": "list_skills",
            "run_id": "run-list-skills",
            "data": {"output": {"ok": True, "skills": [{"name": "ppt-translator"}]}},
        }
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "data": {
                "output": {
                    "answer": "已真实调用 `list_skills`。\n当前返回的 Skill：\n- ppt-translator",
                    "messages": [{"content": ""}],
                }
            },
        }


class _InlineThinkTagStreamingAgent(_DummyAgent):
    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": _Chunk(content="<think>先分析需求。</think>这是最终回复。")},
        }


class _SplitInlineThinkTagStreamingAgent(_DummyAgent):
    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": _Chunk(content="<think>先")},
        }
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": _Chunk(content="分析需求。</think>这是")},
        }
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": _Chunk(content="最终回复。")},
        }


class _UsageMessage:
    def __init__(self):
        self.content = "ok"
        self.usage_metadata = {
            "input_tokens": 8,
            "output_tokens": 13,
            "total_tokens": 21,
            "input_token_details": {},
            "output_token_details": {"reasoning": 5},
        }


class _UsageAgent(_DummyAgent):
    async def ainvoke(self, state, config=None, context=None):
        self.last_ainvoke_state = state
        self.last_ainvoke_context = context
        self.last_ainvoke_config = config
        return {"messages": [_UsageMessage()]}


class _UsageStateStreamingAgent(_StreamingAgent):
    def get_state(self, config):
        del config
        return SimpleNamespace(values={"messages": [_UsageMessage()]}, config=self.state_config)


class _FinalOutputUsageStreamingAgent(_DummyAgent):
    async def astream_events(self, state, version="v2", config=None):
        self.last_astream_state = state
        self.last_astream_config = config
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "data": {"output": {"answer": "final only", "messages": [_UsageMessage()]}},
        }


def _make_runner(module=None) -> LangGraphRunner:
    detection = SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent")
    runner = LangGraphRunner(detection, ".")
    runner._agent = _DummyAgent()
    if module is not None:
        runner._module = module
    return runner


def _make_streaming_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _StreamingAgent()
    return runner


def _make_duplicated_reasoning_streaming_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _DuplicatedReasoningStreamingAgent()
    return runner


def _make_tool_dict_output_streaming_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _ToolDictOutputStreamingAgent()
    return runner


def _make_tool_then_answer_streaming_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _ToolThenAnswerStreamingAgent()
    return runner


def _make_inline_think_tag_streaming_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _InlineThinkTagStreamingAgent()
    return runner


def _make_split_inline_think_tag_streaming_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _SplitInlineThinkTagStreamingAgent()
    return runner


def _make_usage_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _UsageAgent()
    return runner


def _make_usage_state_streaming_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _UsageStateStreamingAgent()
    return runner


def _make_final_output_usage_streaming_runner() -> LangGraphRunner:
    runner = _make_runner()
    runner._agent = _FinalOutputUsageStreamingAgent()
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
async def test_invoke_simplified_input_does_not_duplicate_current_user_message_when_history_contains_it():
    runner = _make_runner()

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "hello",
            "history": [{"role": "user", "content": "hello"}],
        }
    )

    messages = runner._agent.last_ainvoke_state["messages"]
    user_messages = [
        message
        for message in messages
        if message.__class__.__name__ == "HumanMessage" and message.content == "hello"
    ]
    assert len(user_messages) == 1


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
async def test_invoke_checkpoint_resume_uses_checkpoint_id_and_none_input():
    runner = _make_runner()

    result = await runner.invoke(
        {
            "session_id": "sess-1",
            "checkpoint_resume": True,
            "framework_ref": {
                "langgraph": {
                    "thread_id": "tenant-a:agent-b:sess-1",
                    "checkpoint_id": "ckpt-123",
                }
            },
        }
    )

    assert result["output"] == "ok"
    assert runner._agent.last_ainvoke_state is None
    assert runner._agent.last_ainvoke_config["configurable"] == {
        "thread_id": "tenant-a:agent-b:sess-1",
        "checkpoint_id": "ckpt-123",
    }


@pytest.mark.asyncio
async def test_invoke_checkpoint_resume_preserves_checkpoint_namespace_when_present():
    runner = _make_runner()

    await runner.invoke(
        {
            "session_id": "sess-1",
            "checkpoint_resume": True,
            "framework_ref": {
                "langgraph": {
                    "thread_id": "tenant-a:agent-b:sess-1",
                    "checkpoint_ns": "subgraph-ns",
                    "checkpoint_id": "ckpt-123",
                }
            },
        }
    )

    assert runner._agent.last_ainvoke_config["configurable"] == {
        "thread_id": "tenant-a:agent-b:sess-1",
        "checkpoint_ns": "subgraph-ns",
        "checkpoint_id": "ckpt-123",
    }


@pytest.mark.asyncio
async def test_invoke_reports_latest_langgraph_checkpoint_ref_from_state_config():
    runner = _make_runner()
    runner._agent.state_config = {
        "configurable": {
            "thread_id": "tenant-a:agent-b:sess-1",
            "checkpoint_id": "ckpt-after",
        }
    }

    result = await runner.invoke({"session_id": "tenant-a:agent-b:sess-1", "input": "hello"})

    assert result["metadata"]["agentengine"] == {
        "framework": "langgraph",
        "framework_ref": {
            "langgraph": {
                "thread_id": "tenant-a:agent-b:sess-1",
                "checkpoint_id": "ckpt-after",
            }
        },
    }


@pytest.mark.asyncio
async def test_invoke_reports_checkpoint_namespace_from_state_config_when_present():
    runner = _make_runner()
    runner._agent.state_config = {
        "configurable": {
            "thread_id": "tenant-a:agent-b:sess-1",
            "checkpoint_ns": "subgraph-ns",
            "checkpoint_id": "ckpt-after",
        }
    }

    result = await runner.invoke({"session_id": "tenant-a:agent-b:sess-1", "input": "hello"})

    assert result["metadata"]["agentengine"]["framework_ref"]["langgraph"] == {
        "thread_id": "tenant-a:agent-b:sess-1",
        "checkpoint_ns": "subgraph-ns",
        "checkpoint_id": "ckpt-after",
    }


@pytest.mark.asyncio
async def test_invoke_reports_latest_langgraph_checkpoint_ref_from_async_state_config():
    runner = _make_runner()
    runner._agent = _AsyncStateAgent()
    runner._agent.state_config = {
        "configurable": {
            "thread_id": "tenant-a:agent-b:sess-async",
            "checkpoint_id": "ckpt-async",
        }
    }

    result = await runner.invoke({"session_id": "tenant-a:agent-b:sess-async", "input": "hello"})

    assert result["metadata"]["agentengine"] == {
        "framework": "langgraph",
        "framework_ref": {
            "langgraph": {
                "thread_id": "tenant-a:agent-b:sess-async",
                "checkpoint_id": "ckpt-async",
            }
        },
    }


@pytest.mark.asyncio
async def test_invoke_extracts_usage_from_langchain_message_metadata():
    runner = _make_usage_runner()

    result = await runner.invoke({"session_id": "sess-usage", "input": "hello"})

    assert result["usage"] == {
        "input_tokens": 8,
        "output_tokens": 13,
        "total_tokens": 21,
        "input_token_details": {},
        "output_token_details": {"reasoning": 5},
    }


@pytest.mark.asyncio
async def test_stream_emits_final_usage_from_graph_state_after_text_stream():
    runner = _make_usage_state_streaming_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "sess-usage-stream",
                "input": "hello",
            }
        )
    ]

    assert chunks[-1] == {
        "output": "这是最终回复。",
        "type": "final",
        "usage": {
            "input_tokens": 8,
            "output_tokens": 13,
            "total_tokens": 21,
            "input_token_details": {},
            "output_token_details": {"reasoning": 5},
        },
    }


@pytest.mark.asyncio
async def test_stream_final_output_chunk_includes_usage_from_chain_end_output():
    runner = _make_final_output_usage_streaming_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "sess-final-usage",
                "input": "hello",
            }
        )
    ]

    assert chunks[-1] == {
        "output": "final only",
        "type": "final",
        "usage": {
            "input_tokens": 8,
            "output_tokens": 13,
            "total_tokens": 21,
            "input_token_details": {},
            "output_token_details": {"reasoning": 5},
        },
    }


@pytest.mark.asyncio
async def test_stream_checkpoint_resume_uses_checkpoint_id_and_none_input():
    runner = _make_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "sess-1",
                "checkpoint_resume": True,
                "framework_ref": {
                    "langgraph": {
                        "checkpoint_id": "ckpt-456",
                    }
                },
            }
        )
    ]

    assert chunks and chunks[-1]["type"] == "final"
    assert runner._agent.last_astream_state is None
    assert runner._agent.last_astream_config["configurable"] == {
        "thread_id": "sess-1",
        "checkpoint_id": "ckpt-456",
    }


@pytest.mark.asyncio
async def test_stream_reports_latest_langgraph_checkpoint_ref_from_state_config():
    runner = _make_streaming_runner()
    runner._agent.state_config = {
        "configurable": {
            "thread_id": "tenant-a:agent-b:sess-1",
            "checkpoint_id": "ckpt-stream",
        }
    }

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "tenant-a:agent-b:sess-1",
                "input": "hello",
            }
        )
    ]

    assert chunks[-1] == {
        "type": "checkpoint",
        "metadata": {
            "agentengine": {
                "framework": "langgraph",
                "framework_ref": {
                    "langgraph": {
                        "thread_id": "tenant-a:agent-b:sess-1",
                        "checkpoint_id": "ckpt-stream",
                    }
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_stream_does_not_mix_reasoning_into_final_text():
    runner = _make_streaming_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "s1",
                "input": "写一个python快排的示例",
            }
        )
    ]

    assert chunks[:-1] == [
        {"delta": "先分析需求。", "type": "thinking"},
        {"delta": "这是最终回复。", "type": "text"},
    ]
    assert chunks[-1] == {"output": "这是最终回复。", "type": "final"}
    assert all("先分析需求。" not in chunk.get("delta", "") for chunk in chunks if chunk["type"] == "text")


@pytest.mark.asyncio
async def test_stream_ignores_content_when_chunk_duplicates_reasoning():
    runner = _make_duplicated_reasoning_streaming_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "s1",
                "input": "写一个python快排的示例",
            }
        )
    ]

    assert chunks[:-1] == [
        {"delta": "先分析需求。", "type": "thinking"},
        {"delta": "这是最终回复。", "type": "text"},
    ]
    assert chunks[-1] == {"output": "这是最终回复。", "type": "final"}


@pytest.mark.asyncio
async def test_stream_extracts_inline_think_tags_from_content():
    runner = _make_inline_think_tag_streaming_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "s1",
                "input": "写一个python快排的示例",
            }
        )
    ]

    assert chunks[:-1] == [
        {"delta": "先分析需求。", "type": "thinking"},
        {"delta": "这是最终回复。", "type": "text"},
    ]
    assert chunks[-1] == {"output": "这是最终回复。", "type": "final"}


@pytest.mark.asyncio
async def test_stream_extracts_split_inline_think_tags_from_content():
    runner = _make_split_inline_think_tag_streaming_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "s1",
                "input": "写一个python快排的示例",
            }
        )
    ]

    thinking_deltas = [chunk["delta"] for chunk in chunks if chunk["type"] == "thinking"]
    text_deltas = [chunk["delta"] for chunk in chunks if chunk["type"] == "text"]

    assert thinking_deltas == ["先分析需求。"]
    assert "".join(text_deltas) == "这是最终回复。"
    assert all("<think" not in chunk.get("delta", "") for chunk in chunks)


@pytest.mark.asyncio
async def test_stream_preserves_dict_tool_output_for_gateway_approval_bridge():
    runner = _make_tool_dict_output_streaming_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "s1",
                "input": "写文件",
            }
        )
    ]

    assert chunks == [
        {
            "type": "tool_result",
            "tool_name": "write_workspace_file",
            "tool_args": {},
            "tool_output": {
                "ok": False,
                "type": "approval_required",
                "approval_request": {
                    "id": "appr_write",
                    "tool_name": "write_workspace_file",
                },
            },
            "run_id": "run-approval",
        }
    ]


@pytest.mark.asyncio
async def test_stream_emits_final_answer_after_tool_events_without_text_stream():
    runner = _make_tool_then_answer_streaming_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {
                "session_id": "s1",
                "input": "你有哪些 skill",
            }
        )
    ]

    assert chunks[-1] == {
        "type": "final",
        "output": "已真实调用 `list_skills`。\n当前返回的 Skill：\n- ppt-translator",
    }
    assert [chunk["type"] for chunk in chunks] == ["tool_call", "tool_result", "final"]


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


@pytest.mark.asyncio
async def test_invoke_with_image_attachment_converts_to_multimodal_human_message(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    runner = _make_runner()
    image_path = tmp_path / ".agentengine" / "ui" / "files" / "diagram.png"
    image_bytes = b"\x89PNG\r\n\x1a\nfake-image"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(image_bytes)

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "请分析这张图片",
            "model_metadata": {
                "id": "kimi-k2.6",
                "architecture": {"input_modalities": ["文字", "图片"]},
            },
            "attachments": [
                {
                    "display_name": "diagram.png",
                    "mime_type": "image/png",
                    "transport": "reference",
                    "file_uri": "ksadk-upload://img123",
                    "storage_path": str(image_path),
                }
            ],
        }
    )

    content = runner._agent.last_ainvoke_state["messages"][-1].content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请分析这张图片"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    )


@pytest.mark.asyncio
async def test_invoke_with_inline_image_attachment_converts_to_multimodal_human_message():
    runner = _make_runner()
    image_b64 = base64.b64encode(b"fake-inline-image").decode("ascii")

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "请看图",
            "model_metadata": {
                "id": "kimi-k2.6",
                "architecture": {"input_modalities": ["文字", "图片"]},
            },
            "attachments": [
                {
                    "display_name": "photo.jpg",
                    "mime_type": "image/jpeg",
                    "transport": "inline",
                    "data": image_b64,
                }
            ],
        }
    )

    content = runner._agent.last_ainvoke_state["messages"][-1].content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请看图"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
    }


@pytest.mark.asyncio
async def test_invoke_with_remote_image_attachment_preserves_image_url_for_multimodal_model():
    runner = _make_runner()
    image_url = "https://example.com/photo.png"

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "请看图",
            "model_metadata": {
                "id": "kimi-k2.6",
                "architecture": {"input_modalities": ["文字", "图片"]},
            },
            "attachments": [
                {
                    "display_name": "photo.png",
                    "mime_type": "image/*",
                    "transport": "reference",
                    "file_uri": image_url,
                }
            ],
        }
    )

    content = runner._agent.last_ainvoke_state["messages"][-1].content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请看图"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": image_url},
    }


@pytest.mark.asyncio
async def test_invoke_with_image_attachment_keeps_image_block_even_when_catalog_is_stale(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    runner = _make_runner()
    image_path = tmp_path / ".agentengine" / "ui" / "files" / "diagram.png"
    image_bytes = b"\x89PNG\r\n\x1a\nfake-image"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(image_bytes)

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "请分析这张图片",
            "model_metadata": {
                "id": "glm-5.1",
                "architecture": {"input_modalities": ["文字"]},
            },
            "attachments": [
                {
                    "display_name": "diagram.png",
                    "mime_type": "image/png",
                    "transport": "reference",
                    "file_uri": "ksadk-upload://img123",
                    "storage_path": str(image_path),
                }
            ],
        }
    )

    content = runner._agent.last_ainvoke_state["messages"][-1].content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请分析这张图片"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    )


def test_extract_output_prefers_explicit_output_over_messages_tail():
    runner = _make_runner()

    output = runner._extract_output(
        {
            "output": "业务最终回答",
            "messages": [{"role": "system", "content": "系统提示词"}],
        }
    )

    assert output == "业务最终回答"


def test_extract_output_uses_langgraph_answer_field():
    runner = _make_runner()

    output = runner._extract_output(
        {
            "answer": "业务最终回答",
            "messages": [{"role": "assistant", "content": ""}],
        }
    )

    assert output == "业务最终回答"


@pytest.mark.asyncio
async def test_invoke_simplified_input_prepends_ambient_kb_and_memory_context():
    runner = _make_runner()

    await runner.invoke(
        {
            "session_id": "s1",
            "input": "继续回答",
            "kb_context": {"formatted_text": "知识库: 当前支持标准型实例"},
            "memory_context": {"formatted_text": "记忆: 用户关注机型价格"},
            "platform_context": {"agent_id": "demo-agent", "user_id": "user-1"},
        }
    )

    state = runner._agent.last_ainvoke_state
    assert "messages" in state
    assert state["messages"][0].__class__.__name__ == "SystemMessage"
    assert "知识库: 当前支持标准型实例" in state["messages"][0].content
    assert "记忆: 用户关注机型价格" in state["messages"][0].content
    assert state["messages"][-1].content == "继续回答"
    assert runner._agent.last_ainvoke_context == {
        "agent_id": "demo-agent",
        "user_id": "user-1",
    }


@pytest.mark.asyncio
async def test_invoke_messages_payload_injects_system_context_message():
    runner = _make_runner()

    await runner.invoke(
        {
            "session_id": "s1",
            "messages": [],
            "kb_context": {"formatted_text": "KB facts"},
            "memory_context": {"formatted_text": "Memory facts"},
        }
    )

    state = runner._agent.last_ainvoke_state
    assert len(state["messages"]) == 1
    first = state["messages"][0]
    assert first.__class__.__name__ == "SystemMessage"
    assert "KB facts" in first.content
    assert "Memory facts" in first.content


# ---- ksadk_prepare_state hook tests ----


@pytest.mark.asyncio
async def test_invoke_uses_ksadk_prepare_state_hook():
    def ksadk_prepare_state(payload, session_context):
        return {
            "query": payload["input"],
            "results": [],
            "session_id": session_context["session_id"],
        }

    runner = _make_runner(module=SimpleNamespace(ksadk_prepare_state=ksadk_prepare_state))
    await runner.invoke({"session_id": "s1", "input": "hello"})

    state = runner._agent.last_ainvoke_state
    assert state == {"query": "hello", "results": [], "session_id": "s1"}
    assert "messages" not in state


@pytest.mark.asyncio
async def test_invoke_prepare_state_hook_receives_kb_and_memory_context():
    captured = []

    def ksadk_prepare_state(payload, session_context):
        captured.append((payload, session_context))
        return {"query": payload["input"]}

    runner = _make_runner(module=SimpleNamespace(ksadk_prepare_state=ksadk_prepare_state))
    await runner.invoke({
        "session_id": "s1",
        "input": "search",
        "kb_context": {"formatted_text": "KB facts"},
        "memory_context": {"formatted_text": "Memory facts"},
        "platform_context": {"agent_id": "a1", "user_id": "u1"},
    })

    payload, session_context = captured[0]
    assert payload == {"input": "search"}
    assert session_context["kb_context"] == {"formatted_text": "KB facts"}
    assert session_context["memory_context"] == {"formatted_text": "Memory facts"}
    assert session_context["platform_context"] == {"agent_id": "a1", "user_id": "u1"}
    assert session_context["is_resume"] is False
    state = runner._agent.last_ainvoke_state
    assert state == {"query": "search"}


@pytest.mark.asyncio
async def test_invoke_prepare_state_hook_receives_full_normalized_payload():
    captured = []

    def ksadk_prepare_state(payload, session_context):
        captured.append((payload, session_context))
        return {"query": payload["input"], "files": payload["files"]}

    runner = _make_runner(module=SimpleNamespace(ksadk_prepare_state=ksadk_prepare_state))
    await runner.invoke(
        {
            "session_id": "s1",
            "input": "search",
            "history": [{"role": "user", "content": "old"}],
            "files": [{"name": "a.txt"}],
            "attachments": [{"display_name": "a.txt"}],
            "platform_context": {"agent_id": "a1"},
        }
    )

    payload, session_context = captured[0]
    assert payload["input"] == "search"
    assert payload["files"] == [{"name": "a.txt"}]
    assert payload["attachments"] == [{"display_name": "a.txt"}]
    assert "session_id" not in payload
    assert "history" not in payload
    assert "platform_context" not in payload
    assert session_context["history"] == [{"role": "user", "content": "old"}]
    assert runner._agent.last_ainvoke_state == {"query": "search", "files": [{"name": "a.txt"}]}


@pytest.mark.asyncio
async def test_invoke_resume_with_prepare_state_hook():
    captured = []

    def ksadk_prepare_state(payload, session_context):
        captured.append(session_context)
        return {
            "approved": payload["input"].get("approved", False),
            "comment": payload["input"].get("comment", ""),
        }

    runner = _make_runner(module=SimpleNamespace(ksadk_prepare_state=ksadk_prepare_state))
    await runner.invoke({
        "session_id": "s1",
        "resume": True,
        "input": {"approved": True, "comment": "looks good"},
    })

    state = runner._agent.last_ainvoke_state
    assert isinstance(state, Command)
    assert state.resume == {"approved": True, "comment": "looks good"}
    assert captured[0]["is_resume"] is True


@pytest.mark.asyncio
async def test_invoke_without_hook_uses_to_state():
    runner = _make_runner()
    await runner.invoke({"session_id": "s1", "input": "hello"})
    state = runner._agent.last_ainvoke_state
    assert "messages" in state
    assert state["messages"][-1].content == "hello"


@pytest.mark.asyncio
async def test_invoke_hook_returns_non_dict_raises_type_error():
    def ksadk_prepare_state(payload, session_context):
        return "not a dict"

    runner = _make_runner(module=SimpleNamespace(ksadk_prepare_state=ksadk_prepare_state))
    with pytest.raises(TypeError, match="ksadk_prepare_state"):
        await runner.invoke({"session_id": "s1", "input": "hello"})
