from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runners.adk_runner import ADKRunner
from ksadk.runtime.adapter import RunHandle, StartRequest
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


def _build_a2a_runner_input(metadata: dict | None = None) -> dict:
    """经新架构 A2A 入口构造 runner_input。

    runtime-event-v2 重构后,A2A ``context.metadata`` 不再走旧 ``A2ARuntimeExecutor``
    的 ``_build_runner_input``,而是:``task_adapter.start_task`` 把它放进
    ``StartRequest.metadata``,再由 ``RunnerRuntimeAdapter._build_runner_input`` 落入
    ``runner_input["metadata"]``。这里复刻这条入口链路的末段(StartRequest 之后)。
    """
    adapter = RunnerRuntimeAdapter.__new__(RunnerRuntimeAdapter)
    adapter._active_runs = {}  # noqa: SLF001  # 无 resume/prepared 覆盖,走默认构造路径
    handle = RunHandle(run_id="task-1", session_id="ctx-1", runtime_type="adk")
    request = StartRequest(
        input="你好",
        user_id="tenant-u",
        session_id="ctx-1",
        metadata=dict(metadata or {}),
    )
    return adapter._build_runner_input(handle, request)  # noqa: SLF001


def test_metadata_flows_from_a2a_to_state_delta():
    """父 Agent 通过 A2A metadata 注入的自定义参数应能在子 Agent 的 tool_context.state 中读取。"""
    runner = ADKRunner.__new__(ADKRunner)

    # A2A 层(新架构): context.metadata → runner_input["metadata"]
    runner_input = _build_a2a_runner_input(metadata={"real_user_id": "user-123"})
    assert runner_input["metadata"] == {"real_user_id": "user-123"}

    # ADK 层: runner_input → state_delta（修复前 metadata 被丢弃）
    state_delta = runner._build_state_delta(runner_input)  # noqa: SLF001
    assert state_delta["metadata"] == {"real_user_id": "user-123"}

    # 白名单行为: 已白名单字段透传, 无关字段过滤
    raw = {"input_parts": ["p1"], "metadata": {"k": "v"}, "unknown": "x"}
    sd = runner._build_state_delta(raw)  # noqa: SLF001
    assert sd == {"input_parts": ["p1"], "metadata": {"k": "v"}}


@pytest.mark.asyncio
async def test_stream_preserves_ids_for_repeated_calls_to_the_same_tool():
    runner = ADKRunner.__new__(ADKRunner)
    runner._agent = SimpleNamespace(name="root")  # noqa: SLF001
    runner._prepare_trace_metadata = lambda _session_id: (None, [], None, "root")  # noqa: SLF001
    runner._extract_event_usage = lambda _event: {}  # noqa: SLF001
    runner._extract_approval_signals = lambda _event: []  # noqa: SLF001

    async def ensure_session(_session_id):
        return "session-1"

    async def event_stream():
        for call_id in ("call-1", "call-2"):
            call = SimpleNamespace(
                id=call_id,
                name="metaso_web_reader",
                args={"url": "https://example.test/article"},
            )
            yield SimpleNamespace(
                partial=False,
                author="root",
                content=SimpleNamespace(parts=[]),
                actions=None,
                get_function_calls=lambda call=call: [call],
            )

    async def prepare_events(**_kwargs):
        return event_stream()

    runner._ensure_session = ensure_session  # type: ignore[method-assign]  # noqa: SLF001
    runner._prepare_run_events = prepare_events  # type: ignore[method-assign]  # noqa: SLF001

    chunks = [chunk async for chunk in runner.stream({"input": "test"})]
    tool_calls = [chunk for chunk in chunks if chunk.get("type") == "tool_call"]

    assert [chunk["tool_call_id"] for chunk in tool_calls] == ["call-1", "call-2"]
