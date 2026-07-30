from __future__ import annotations

from a2a.server.agent_execution import RequestContext
from a2a.server.agent_execution.context import ServerCallContext
from a2a.types import SendMessageRequest
from google.protobuf.json_format import ParseDict

from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.runners.adk_runner import ADKRunner


def _build_request_context(metadata: dict | None = None) -> RequestContext:
    payload: dict = {
        "message": {
            "messageId": "msg-1",
            "role": "ROLE_USER",
            "parts": [{"text": "你好"}],
            "taskId": "task-1",
            "contextId": "ctx-1",
        },
    }
    if metadata is not None:
        payload["metadata"] = metadata
    params = ParseDict(payload, SendMessageRequest())
    call_context = ServerCallContext(state={})
    return RequestContext(call_context=call_context, request=params)


def test_metadata_flows_from_a2a_to_state_delta():
    """父 Agent 通过 A2A metadata 注入的自定义参数应能在子 Agent 的 tool_context.state 中读取。"""
    ctx = _build_request_context(metadata={"real_user_id": "user-123"})
    executor = A2ARuntimeExecutor(runner=ADKRunner.__new__(ADKRunner))
    runner = ADKRunner.__new__(ADKRunner)

    # A2A 层: context.metadata → runner_input
    runner_input = executor._build_runner_input(ctx)  # noqa: SLF001
    assert runner_input["metadata"] == {"real_user_id": "user-123"}

    # ADK 层: runner_input → state_delta（修复前 metadata 被丢弃）
    state_delta = runner._build_state_delta(runner_input)  # noqa: SLF001
    assert state_delta["metadata"] == {"real_user_id": "user-123"}

    # 白名单行为: 已白名单字段透传, 无关字段过滤
    raw = {"input_parts": ["p1"], "metadata": {"k": "v"}, "unknown": "x"}
    sd = runner._build_state_delta(raw)  # noqa: SLF001
    assert sd == {"input_parts": ["p1"], "metadata": {"k": "v"}}
