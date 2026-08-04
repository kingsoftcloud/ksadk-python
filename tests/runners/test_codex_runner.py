"""CodexRunner 单测:用 fake AsyncCodexClient 不打网络,测 stream 反投射 + invoke + cancel。"""

import asyncio
from pathlib import Path
from queue import SimpleQueue

import pytest

from ksadk.codex.client import CodexClient
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runners.codex_runner import CodexRunner


class _FakeCodex(CodexClient):
    """假 codex 后端:发 commentary delta + final_answer completed,模拟 codex 流。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.interrupted: list[str] = []
        self.prompts: list[str] = []
        self.thread_configs: list = []  # 记录 start_thread 收到的 config(model/base_instructions)
        self._seq = 0

    async def start_thread(self, config=None) -> str:
        self._seq += 1
        tid = f"thread_{self._seq}"
        self.started.append(tid)
        self.thread_configs.append(config or {})
        return tid

    async def resume_thread(self, thread_id, config=None) -> str:
        return thread_id

    def run_turn(self, thread_id, prompt, *, config=None):
        self.prompts.append(prompt)
        async def gen():
            yield {"method": "item/started",
                   "params": {"item": {"id": "cmd1", "type": "commandExecution",
                                         "command": "sed -n '1,80p' src/demo.py",
                                         "cwd": "/workspace", "commandActions": [],
                                         "status": "inProgress"}}}
            yield {"method": "item/completed",
                   "params": {"item": {"id": "cmd1", "type": "commandExecution",
                                         "command": "sed -n '1,80p' src/demo.py",
                                         "cwd": "/workspace", "commandActions": [],
                                         "status": "completed", "exitCode": 0,
                                         "durationMs": 8, "aggregatedOutput": "source"}}}
            # 真实 codex 流:item/started(建 phase 上下文) → delta(多个) → completed
            yield {"method": "item/started",
                   "params": {"item": {"id": "rs1", "type": "reasoning",
                                       "phase": "commentary"}}}
            yield {"method": "item/agentMessage/delta",
                   "params": {"delta": "想一下", "item_id": "rs1"}}
            yield {"method": "item/completed",
                   "params": {"item": {"id": "rs1", "type": "reasoning", "phase": "commentary",
                                       "summary": ["想一下"]}}}
            yield {"method": "item/started",
                   "params": {"item": {"id": "m1", "type": "agentMessage",
                                       "phase": "final_answer"}}}
            yield {"method": "item/agentMessage/delta",
                   "params": {"delta": "你好", "item_id": "m1"}}
            yield {"method": "item/completed",
                   "params": {"item": {"id": "m1", "type": "agentMessage",
                                       "phase": "final_answer", "text": "你好"}}}

        return gen()

    async def interrupt_active_turn(self, thread_id) -> bool:
        self.interrupted.append(thread_id)
        return True

    async def close(self) -> None:
        return None


def _make_runner(monkeypatch):
    # 跳过 AsyncCodexClient 真实 SDK 构造(它 lazy import openai_codex),直接注入 fake
    runner = CodexRunner.__new__(CodexRunner)
    runner.detection_result = type("D", (), {"name": "codex-agent", "type": None})()
    runner.project_dir = "."
    runner._agent = None
    runner._client = _FakeCodex()
    runner._runtime = CodexRuntimeAdapter(runner._client, sandbox_read_only=True)
    runner._handles = {}
    runner._proxy_events = SimpleQueue()
    return runner


def test_stream_projects_text_and_thinking():
    runner = _make_runner(None)
    chunks = asyncio.run(_collect(runner.stream({"input": "hi", "session_id": "s1"})))
    types = [c.get("type") for c in chunks]
    # commentary delta -> thinking;final_answer -> text;末尾 RUN_COMPLETED -> final
    assert "thinking" in types
    assert "text" in types
    assert types[-1] == "final"
    final = chunks[-1]
    assert "你好" in final["output"]  # accumulated 文本


def test_runtime_event_stream_preserves_the_canonical_contract():
    runner = _make_runner(None)

    events = asyncio.run(
        _collect(runner.stream_runtime_events({"input": "hi", "session_id": "s-native"}))
    )

    assert all(isinstance(event, RuntimeEvent) for event in events)
    assert EventType.TOOL_CALL_BEGIN in [event.event_type for event in events]
    assert EventType.TEXT_COMPLETED in [event.event_type for event in events]
    assert events[-1].event_type == EventType.RUN_COMPLETED


def test_stream_projects_codex_command_events_without_flattening_them_to_text():
    runner = _make_runner(None)
    chunks = asyncio.run(_collect(runner.stream({"input": "review", "session_id": "s-command"})))

    command_events = [chunk for chunk in chunks if chunk.get("type") == "tool"]
    assert command_events == [
        {
            "type": "tool",
            "status": "started",
            "call_id": "cmd1",
            "name": "codex.command",
            "args": {
                "command": "sed -n '1,80p' src/demo.py",
                "cwd": "/workspace",
                "command_actions": [],
            },
        },
        {
            "type": "tool",
            "status": "completed",
            "call_id": "cmd1",
            "name": "codex.command",
            "result": {
                "status": "completed",
                "exit_code": 0,
                "duration_ms": 8,
                "output": "source",
            },
        },
    ]


def test_stream_projects_runtime_reported_usage_without_estimation():
    """Break caught: exact Runtime usage is consumed internally but never reaches Studio."""

    class _UsageCodex(_FakeCodex):
        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "thread_id": thread_id,
                        "turn_id": "turn-1",
                        "token_usage": {
                            "last": {
                                "input_tokens": 128,
                                "cached_input_tokens": 16,
                                "output_tokens": 32,
                                "reasoning_output_tokens": 8,
                                "total_tokens": 160,
                            }
                        },
                    },
                }

            return gen()

    runner = _make_runner(None)
    runner._client = _UsageCodex()
    runner._runtime = CodexRuntimeAdapter(runner._client, sandbox_read_only=True)

    chunks = asyncio.run(_collect(runner.stream({"input": "hi", "session_id": "s-usage"})))

    assert next(chunk for chunk in chunks if chunk.get("type") == "usage") == {
        "type": "usage",
        "usage": {
            "input_tokens": 128,
            "cached_tokens": 16,
            "output_tokens": 32,
            "reasoning_tokens": 8,
            "total_tokens": 160,
            "source": "codex",
        },
    }


def test_invoke_aggregates_final_output():
    runner = _make_runner(None)
    result = asyncio.run(runner.invoke({"input": "hi", "session_id": "s2"}))
    assert "你好" in result["output"]


def test_load_agent_is_noop():
    runner = _make_runner(None)
    runner.load_agent()  # 不抛异常即通过
    assert runner._agent is True


def test_stream_passes_model_and_prompt_to_thread():
    """C:yaml 的 model/prompt 经 raw_config 传给 codex thread(model + base_instructions)。"""
    runner = _make_runner(None)
    # 模拟 detector 从 ksadk.yaml 读出的 raw_config
    runner.detection_result.raw_config = {"model": "glm-5.2", "prompt": "你是编码助手"}
    asyncio.run(_collect(runner.stream({"input": "hi", "session_id": "s9"})))
    cfg = runner._client.thread_configs[-1]
    assert cfg["model"] == "glm-5.2"
    assert cfg["base_instructions"] == "你是编码助手"
    assert cfg["sandbox_read_only"] is True
    assert cfg["cwd"] == str(Path(".").resolve())


def test_stream_input_model_overrides_yaml():
    """C:本轮请求的 model 优先于 yaml(raw_config)。"""
    runner = _make_runner(None)
    runner.detection_result.raw_config = {"model": "yaml-model"}
    asyncio.run(_collect(runner.stream({"input": "hi", "session_id": "s10", "model": "glm-5.1"})))
    cfg = runner._client.thread_configs[-1]
    assert cfg["model"] == "glm-5.1"


def test_stream_includes_previous_turns_in_codex_prompt():
    """Ephemeral Codex thread must still receive Studio session history."""
    runner = _make_runner(None)
    asyncio.run(
        _collect(
            runner.stream(
                {
                    "input": "第二轮问题",
                    "session_id": "s-history",
                    "history": [
                        {"role": "user", "content": "第一轮问题"},
                        {"role": "assistant", "content": "第一轮回答"},
                    ],
                }
            )
        )
    )

    assert runner._client.prompts[-1] == (
        "以下是同一会话的历史消息，仅用于保持上下文：\n\n"
        "用户：第一轮问题\n\n助手：第一轮回答\n\n"
        "当前用户消息：\n第二轮问题"
    )


def test_stream_projects_proxy_observability_without_credentials():
    runner = _make_runner(None)
    runner._observe_proxy(
        "proxy.requested",
        {
            "responseId": "resp-1",
            "model": "glm-5.2",
            "protocol": "responses-to-chat",
            "stream": True,
        },
    )
    chunks = asyncio.run(_collect(runner.stream({"input": "hi", "session_id": "s-proxy"})))

    proxy = next(chunk for chunk in chunks if chunk.get("type") == "proxy")
    assert proxy == {
        "type": "proxy",
        "event": "proxy.requested",
        "data": {
            "responseId": "resp-1",
            "model": "glm-5.2",
            "protocol": "responses-to-chat",
            "stream": True,
        },
    }


def test_constructor_wires_observer_into_real_client_boundary(monkeypatch, tmp_path):
    captured = {}

    class _Client(_FakeCodex):
        def __init__(self, *, proxy_observer=None):
            super().__init__()
            captured["observer"] = proxy_observer

    monkeypatch.setattr("ksadk.runners.codex_runner.AsyncCodexClient", _Client)
    detection = type("D", (), {"name": "codex-agent", "type": None, "raw_config": {}})()

    runner = CodexRunner(detection, str(tmp_path))

    assert captured["observer"] == runner._observe_proxy


def test_cancelling_stream_interrupts_active_codex_turn():
    class _BlockingCodex(_FakeCodex):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()
            self.turn_closed = asyncio.Event()

        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                try:
                    yield {
                        "method": "item/started",
                        "params": {"item": {"id": "r1", "type": "reasoning"}},
                    }
                    await self.release.wait()
                finally:
                    self.turn_closed.set()

            return gen()

    runner = _make_runner(None)
    client = _BlockingCodex()
    runner._client = client
    runner._runtime = CodexRuntimeAdapter(runner._client, sandbox_read_only=True)

    async def scenario():
        task = asyncio.create_task(
            _collect(runner.stream({"input": "review", "session_id": "s-cancel"}))
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert client.turn_closed.is_set()

    asyncio.run(scenario())
    assert runner._client.interrupted == ["thread_1"]


async def _collect(agen):
    out = []
    async for c in agen:
        out.append(c)
    return out
