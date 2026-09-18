from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

from ksadk.studio.contracts import ModelParameters, NetworkPolicy, ResolvedModel
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import (
    CredentialResolver,
    NetworkGuard,
    OpenAICompatibleModelClient,
)


class AllowNetwork:
    async def check(self, _endpoint_url, _policy):
        return None


def _model() -> ResolvedModel:
    return ResolvedModel(
        provider="openai-compatible",
        model="glm-5.1",
        endpoint_url="https://model.example.com/v1/chat/completions",
        credential_ref="env://MODEL_API_KEY",
        parameters=ModelParameters(temperature=0.1, max_tokens=64),
    )


@pytest.mark.asyncio
async def test_model_client_sends_openai_compatible_request(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        _model(),
        messages=[{"role": "user", "content": "test"}],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert captured["authorization"] == "Bearer secret-value"
    assert captured["json"]["model"] == "glm-5.1"
    assert captured["json"]["stream"] is False
    assert result.content == "OK"
    assert result.usage.total_tokens == 4


@pytest.mark.asyncio
async def test_model_client_streams_chat_deltas_without_buffering(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        body = (
            b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"B"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    chunks = [
        chunk
        async for chunk in client.stream(
            _model(),
            messages=[{"role": "user", "content": "test"}],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
        )
    ]
    assert captured["json"]["stream"] is True
    assert captured["json"]["tools"][0]["function"]["name"] == "lookup"
    assert [chunk.text for chunk in chunks if chunk.text] == ["A", "B"]
    assert chunks[-1].done is True


@pytest.mark.asyncio
async def test_model_client_stops_repetitive_stream_output(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    repeated = "Codex 调研失败，我将重新委派这个任务。"
    body = b"".join(
        (
            "data: "
            + __import__("json").dumps(
                {"choices": [{"delta": {"content": repeated}}]},
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode()
        for _ in range(8)
    )
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )
        ),
    )

    with pytest.raises(StudioError) as captured:
        _ = [
            chunk
            async for chunk in client.stream(
                _model(),
                messages=[],
                network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
                timeout_seconds=10,
            )
        ]

    assert captured.value.code == "MODEL_REPETITIVE_OUTPUT"


@pytest.mark.asyncio
async def test_model_client_recovers_streamed_dsml_as_declared_tool_call(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")

    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"content":"<｜｜DS"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"ML｜｜calls><｜｜DSML｜｜invoke '
            'name=\\"WebSearch\\"><query>official docs</query>"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"<｜｜DSML｜｜/invoke>'
            '<｜｜DSML｜｜/calls>"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "parameters": {"type": "object"},
            },
        }
    ]
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(), transport=httpx.MockTransport(handler)
    )
    chunks = [
        chunk
        async for chunk in client.stream(
            _model(),
            messages=[{"role": "user", "content": "research"}],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            tools=tools,
        )
    ]

    assert not any("DSML" in chunk.text for chunk in chunks)
    assert chunks[-1].done is True
    assert chunks[-1].tool_calls[0].name == "web_search"
    assert __import__("json").loads(chunks[-1].tool_calls[0].arguments) == {
        "query": "official docs"
    }


@pytest.mark.asyncio
async def test_model_client_recovers_spaced_single_pipe_dsml_variant(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    body = (
        'data: {"choices":[{"delta":{"content":"<| DSML | calls><| DSML | invoke '
        'name=\\"write_file\\"><| DSML | parameter name=\\"path\\" '
        'string=\\"true\\">report.md<| DSML | /parameter>"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"<| DSML | parameter name=\\"content\\" '
        'string=\\"true\\"># Report<| DSML | /parameter><| DSML | /invoke>'
        '<| DSML | /calls>"},"finish_reason":"stop"}]}\n\n'
    ).encode()
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        ),
    )
    chunks = [
        chunk
        async for chunk in client.stream(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "write_workspace_file",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
    ]

    assert not any("DSML" in chunk.text for chunk in chunks)
    assert chunks[-1].tool_calls[0].name == "write_workspace_file"
    assert __import__("json").loads(chunks[-1].tool_calls[0].arguments) == {
        "path": "report.md",
        "content": "# Report",
    }


@pytest.mark.asyncio
async def test_model_client_recovers_buffered_dsml_as_declared_tool_call(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                '<||DSML||calls><||DSML||invoke name="delegate_task">'
                                '<||DSML||parameter name="label" string="true">调研 ADK'
                                "<||DSML||/parameter><||DSML||parameter "
                                'name="task_kind" string="true">general'
                                "<||DSML||/parameter><||DSML||/invoke><||DSML||/calls>"
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(), transport=httpx.MockTransport(handler)
    )
    result = await client.complete(
        _model(),
        messages=[{"role": "user", "content": "research"}],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0].name == "delegate_task"
    assert __import__("json").loads(result.tool_calls[0].arguments) == {
        "label": "调研 ADK",
        "task_kind": "general",
    }


@pytest.mark.asyncio
async def test_model_client_recovers_streamed_glm_textual_tool_call(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    fragments = (
        "重试。<tool_",
        "call>delegate_task<arg_key>label</arg_key><arg_value>调研 ADK</arg_value>",
        '<arg_value>task="核验 Google ADK 官方资料"</arg_value>',
        '<arg_key>task_kind="general"</arg_value></tool_call>',
    )
    body = b"".join(
        (
            "data: "
            + __import__("json").dumps(
                {
                    "choices": [
                        {
                            "delta": {"content": fragment},
                            "finish_reason": "stop" if index == len(fragments) - 1 else None,
                        }
                    ]
                },
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode()
        for index, fragment in enumerate(fragments)
    )
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        ),
    )
    chunks = [
        chunk
        async for chunk in client.stream(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "delegate_task", "parameters": {"type": "object"}},
                }
            ],
        )
    ]

    assert "".join(chunk.text for chunk in chunks) == "重试。"
    assert chunks[-1].done is True
    assert chunks[-1].tool_calls[0].name == "delegate_task"
    assert __import__("json").loads(chunks[-1].tool_calls[0].arguments) == {
        "label": "调研 ADK",
        "task": "核验 Google ADK 官方资料",
        "task_kind": "general",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        (
            "准备创建。<tool_call>write_file<arg_value>filename</arg_key>"
            "<arg_value>glm52_write_probe.md</arg_value>content</arg_key>"
            "<arg_value>GLM 5.2 写文件验证</arg_value></think>文件已创建。"
        ),
        (
            "准备创建。<tool_call>write_file_ide49a</arg_value> path</arg_key> "
            "glm52_write_probe.md</arg_value> content</arg_key><arg_value>"
            "GLM 5.2 写文件验证</arg_value></arg_value>"
        ),
        (
            '准备创建。<tool_call>write_file</arg_value>filePath="glm52_write_probe.md"'
            '</arg_value>content="GLM 5.2 写文件验证"</arg_value>文件已创建。'
        ),
    ],
)
async def test_model_client_recovers_real_glm52_malformed_write_call(monkeypatch, content):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    body = (
        "data: "
        + __import__("json").dumps(
            {
                "choices": [
                    {
                        "delta": {"content": content},
                        "finish_reason": "stop",
                    }
                ]
            },
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode()
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        ),
    )
    chunks = [
        chunk
        async for chunk in client.stream(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "write_workspace_file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                }
            ],
        )
    ]

    assert "".join(chunk.text for chunk in chunks) == "准备创建。"
    assert chunks[-1].done is True
    assert chunks[-1].tool_calls[0].name == "write_workspace_file"
    assert __import__("json").loads(chunks[-1].tool_calls[0].arguments) == {
        "path": "glm52_write_probe.md",
        "content": "GLM 5.2 写文件验证",
    }


@pytest.mark.asyncio
async def test_model_client_repairs_glm_residue_in_native_tool_argument_key(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    payloads = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-adk",
                                "function": {
                                    "name": "delegate_task",
                                    "arguments": (
                                        '{"label":"ADK","task</arg_key>":"research ADK",'
                                    ),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"task_kind":"general"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    body = b"".join(
        f"data: {__import__('json').dumps(payload)}\n\n".encode() for payload in payloads
    )
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        ),
    )
    chunks = [
        chunk
        async for chunk in client.stream(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "task": {"type": "string"},
                                "task_kind": {"type": "string"},
                            },
                        },
                    },
                }
            ],
        )
    ]

    assert chunks[-1].done is True
    assert __import__("json").loads(chunks[-1].tool_calls[0].arguments) == {
        "label": "ADK",
        "task": "research ADK",
        "task_kind": "general",
    }


@pytest.mark.asyncio
async def test_model_client_recovers_final_message_tool_call_snapshot(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    payloads = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-write",
                                "function": {"name": "write_workspace_file"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {},
                    "message": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-write",
                                "function": {
                                    "name": "write_workspace_file",
                                    "arguments": {
                                        "path": "report.md",
                                        "content": "# Report\ncomplete",
                                    },
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
    ]
    body = (
        b"".join(f"data: {__import__('json').dumps(payload)}\n\n".encode() for payload in payloads)
        + b"data: [DONE]\n\n"
    )
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        ),
    )
    chunks = [
        chunk
        async for chunk in client.stream(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "write_workspace_file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                }
            ],
        )
    ]

    assert chunks[-1].done is True
    assert chunks[-1].tool_calls[0].id == "call-write"
    assert __import__("json").loads(chunks[-1].tool_calls[0].arguments) == {
        "path": "report.md",
        "content": "# Report\ncomplete",
    }


@pytest.mark.asyncio
async def test_model_client_rejects_reasoning_only_stream(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    body = (
        b'data: {"choices":[{"delta":{"reasoning_content":"still thinking"},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        ),
    )

    with pytest.raises(StudioError) as captured:
        async for _chunk in client.stream(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
        ):
            pass

    assert captured.value.code == "MODEL_EMPTY_RESPONSE"


@pytest.mark.asyncio
async def test_model_client_rejects_dsml_for_undeclared_tool(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    body = (
        b'data: {"choices":[{"delta":{"content":"<||DSML||invoke name=\\"danger\\">"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"<x>1</x><||DSML||/invoke>"},'
        b'"finish_reason":"stop"}]}\n\n'
    )
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        ),
    )

    with pytest.raises(StudioError) as captured:
        _ = [
            chunk
            async for chunk in client.stream(
                _model(),
                messages=[],
                network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
                timeout_seconds=10,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        ]

    assert captured.value.code == "MODEL_TOOL_PROTOCOL_INVALID"


@pytest.mark.asyncio
async def test_model_client_streams_responses_events_and_tool_calls(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        body = (
            b'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
            b'data: {"type":"response.output_item.added","item":'
            b'{"type":"function_call","id":"item_1","call_id":"call_1",'
            b'"name":"lookup"}}\n\n'
            b'data: {"type":"response.function_call_arguments.delta",'
            b'"item_id":"item_1","delta":"{\\"q\\":\\"x\\"}"}\n\n'
            b'data: {"type":"response.output_item.done","item":'
            b'{"type":"function_call","id":"item_1","call_id":"call_1",'
            b'"name":"lookup","arguments":"{\\"q\\":\\"x\\"}"}}\n\n'
            b'data: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":2,"output_tokens":3,"total_tokens":5}}}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    model = _model().model_copy(
        update={"wire_api": "responses", "endpoint_url": "https://model.example.com/v1/responses"}
    )
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(), transport=httpx.MockTransport(handler)
    )
    chunks = [
        chunk
        async for chunk in client.stream(
            model,
            messages=[{"role": "user", "content": "test"}],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
        )
    ]
    assert captured["json"]["stream"] is True
    assert captured["json"]["tools"][0]["name"] == "lookup"
    assert [chunk.text for chunk in chunks if chunk.text] == ["Hi"]
    assert chunks[-2].tool_calls[0].name == "lookup"
    assert chunks[-1].done is True


@pytest.mark.asyncio
async def test_model_client_retries_5xx_without_leaking_secret(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "top-secret")
    attempts = 0
    sleeps = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="upstream failed")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
        )

    async def fake_sleep(value):
        sleeps.append(value)

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )
    result = await client.complete(
        _model(),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=2,
        backoff_seconds=0.5,
    )

    assert result.content == "OK"
    assert attempts == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_model_client_surfaces_rate_limit_without_relabeling_it_as_gateway_failure(
    monkeypatch,
):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(429, text="busy")),
        sleep=lambda _seconds: asyncio.sleep(0),
    )

    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=1,
            backoff_seconds=0,
        )

    assert captured.value.code == "MODEL_RATE_LIMITED"
    assert captured.value.status_code == 429
    assert captured.value.message == "所选生成模型当前限流，请稍后重试或切换模型 Profile"
    assert captured.value.details == {"upstreamStatus": 429}


@pytest.mark.asyncio
async def test_model_client_rejects_redirect(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(302, headers={"Location": "https://attacker.invalid"})
        ),
    )

    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=1,
            backoff_seconds=0,
        )

    assert captured.value.code == "NETWORK_TARGET_DENIED"
    assert "secret-value" not in str(captured.value.as_dict())


@pytest.mark.asyncio
async def test_model_client_rejects_reasoning_only_length_response(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "internal reasoning",
                            },
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"total_tokens": 64},
                },
            )
        ),
    )

    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=1,
            backoff_seconds=0,
        )

    assert captured.value.code == "MODEL_EMPTY_RESPONSE"
    assert captured.value.details == {"finishReason": "length"}


def test_credential_resolver_never_accepts_plaintext_or_missing_env(monkeypatch):
    resolver = CredentialResolver()
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    with pytest.raises(StudioError) as missing:
        resolver.resolve("env://MODEL_API_KEY")
    assert missing.value.code == "SECRET_NOT_FOUND"

    with pytest.raises(StudioError) as invalid:
        resolver.resolve("plain-secret")
    assert invalid.value.code == "SECRET_BACKEND_UNAVAILABLE"


def test_credential_resolver_session_overlay_precedes_environment(monkeypatch):
    resolver = CredentialResolver()
    monkeypatch.setenv("MODEL_API_KEY", "environment-secret")

    assert resolver.status("env://MODEL_API_KEY") == {
        "reference": "env://MODEL_API_KEY",
        "name": "MODEL_API_KEY",
        "configured": True,
        "source": "environment",
        "persistence": "environment",
    }

    configured = resolver.put_session("MODEL_API_KEY", "session-secret")
    assert configured["source"] == "session"
    assert resolver.resolve("env://MODEL_API_KEY") == "session-secret"
    assert "session-secret" not in str(configured)

    restored = resolver.delete_session("MODEL_API_KEY")
    assert restored["source"] == "environment"
    assert resolver.resolve("env://MODEL_API_KEY") == "environment-secret"


def test_credential_resolver_applies_alias_to_workspace_secret(tmp_path: Path):
    """Break caught: Studio sees a persisted model key but Codex receives no auth."""

    from ksadk.studio.workspace import Workspace

    workspace = Workspace(tmp_path)
    workspace.initialize()
    resolver = CredentialResolver(workspace)
    resolver.put_session("AGENTKIT_MODEL_API_KEY", "workspace-model-key")
    resolver = CredentialResolver(workspace)

    assert resolver.resolve("env://OPENAI_API_KEY") == "workspace-model-key"
    status = resolver.status("env://OPENAI_API_KEY")
    assert status["configured"] is True
    assert status["source"] == "workspace-alias"


def test_credential_resolver_prefers_workspace_alias_over_process_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A host OPENAI_API_KEY must not shadow a Studio-configured model credential."""

    from ksadk.studio.workspace import Workspace

    monkeypatch.setenv("OPENAI_API_KEY", "host-primary-key")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    workspace.atomic_write_text(
        ".agentkit/secrets.env",
        "AGENTKIT_MODEL_API_KEY=workspace-profile-key\n",
    )

    resolver = CredentialResolver(workspace)

    assert resolver.resolve("env://OPENAI_API_KEY") == "workspace-profile-key"


def test_credential_resolver_session_overlay_reports_session_over_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """put_session with a workspace configuration must report source == "session"."""

    from ksadk.studio.workspace import Workspace

    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    workspace = Workspace(tmp_path)
    workspace.initialize()
    resolver = CredentialResolver(workspace)

    assert resolver.status("env://MODEL_API_KEY")["source"] == "missing"

    configured = resolver.put_session("MODEL_API_KEY", "session-secret")
    assert configured["source"] == "session"
    assert resolver.status("env://MODEL_API_KEY")["source"] == "session"

    restored = resolver.delete_session("MODEL_API_KEY")
    assert restored["source"] != "session"
    assert resolver.status("env://MODEL_API_KEY")["source"] == "missing"


def test_credential_resolver_rejects_unsafe_session_values():
    resolver = CredentialResolver()

    for value in {"", " leading", "trailing ", "line\nbreak"}:
        with pytest.raises(StudioError) as captured:
            resolver.put_session("MODEL_API_KEY", value)
        assert captured.value.code == "SECRET_VALUE_INVALID"

    with pytest.raises(StudioError) as invalid_name:
        resolver.put_session("lowercase-key", "secret")
    assert invalid_name.value.code == "SECRET_REFERENCE_INVALID"


def test_credential_resolver_clears_all_session_values(monkeypatch):
    resolver = CredentialResolver()
    monkeypatch.delenv("FIRST_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)
    resolver.put_session("FIRST_KEY", "first-secret")
    resolver.put_session("SECOND_KEY", "second-secret")

    resolver.clear_session()

    assert resolver.status("env://FIRST_KEY")["configured"] is False
    assert resolver.status("env://SECOND_KEY")["configured"] is False


@pytest.mark.asyncio
async def test_network_guard_denies_metadata_and_private_dns(monkeypatch):
    guard = NetworkGuard()
    policy = NetworkPolicy(allowed_hosts=["model.example.com"])

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))],
    )
    with pytest.raises(StudioError) as private:
        await guard.check(
            "https://model.example.com/v1/chat/completions",
            policy,
        )
    assert private.value.code == "NETWORK_TARGET_DENIED"

    with pytest.raises(StudioError) as metadata:
        await guard.check(
            "http://169.254.169.254/latest/meta-data",
            NetworkPolicy(
                allowed_hosts=["169.254.169.254"],
                allow_private_network=True,
            ),
        )
    assert metadata.value.code == "NETWORK_TARGET_DENIED"


@pytest.mark.asyncio
async def test_network_guard_allows_explicit_private_target(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8000))
        ],
    )

    await NetworkGuard().check(
        "http://localhost:8000/v1/chat/completions",
        NetworkPolicy(
            allowed_hosts=["localhost"],
            allow_private_network=True,
        ),
    )


@pytest.mark.asyncio
async def test_model_client_sends_response_format_when_allowed(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(
        _model(),
        messages=[{"role": "user", "content": "compose"}],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        response_format={"type": "json_object"},
    )

    assert captured[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_model_client_omits_response_format_when_disabled(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
        )

    model = _model()
    model.parameters = model.parameters.model_copy(update={"allow_json_response_format": False})
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(
        model,
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        response_format={"type": "json_object"},
    )

    assert "response_format" not in captured[0]


@pytest.mark.asyncio
async def test_model_client_retries_400_without_response_format(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        captured.append(payload)
        if "response_format" in payload:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format is not supported"}},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        _model(),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        response_format={"type": "json_object"},
    )

    assert result.content == "{}"
    assert len(captured) == 2
    assert "response_format" in captured[0]
    assert "response_format" not in captured[1]


@pytest.mark.asyncio
async def test_model_client_retries_length_truncation_with_larger_budget(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": ""},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"total_tokens": 64},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"ok": true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 128},
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        _model(),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        retry_on_length=True,
    )

    assert result.content == '{"ok": true}'
    assert len(requests) == 2
    assert requests[0]["max_tokens"] == 64
    assert requests[1]["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_model_client_length_retry_exhausted_raises_empty_response(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"total_tokens": 64},
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=1,
            backoff_seconds=0,
            retry_on_length=True,
        )
    assert captured.value.code == "MODEL_EMPTY_RESPONSE"
    assert captured.value.details == {"finishReason": "length"}


@pytest.mark.asyncio
async def test_model_client_no_length_retry_by_default(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    calls = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"total_tokens": 64},
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=2,
            backoff_seconds=0,
        )
    assert captured.value.code == "MODEL_EMPTY_RESPONSE"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_model_client_omits_unset_sampling_parameters(monkeypatch):
    """未显式配置的 temperature/max_tokens/top_p 一律不出现在 payload。"""

    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
                ]
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(
        ResolvedModel(
            provider="openai-compatible",
            model="kimi-k2",
            endpoint_url="https://model.example.com/v1/chat/completions",
            credential_ref="env://MODEL_API_KEY",
            parameters=ModelParameters(),
        ),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    body = captured["json"]
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert "top_p" not in body


@pytest.mark.asyncio
async def test_model_client_sends_explicitly_configured_sampling_parameters(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
                ]
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(
        ResolvedModel(
            provider="openai-compatible",
            model="glm-5-3",
            endpoint_url="https://model.example.com/v1/chat/completions",
            credential_ref="env://MODEL_API_KEY",
            parameters=ModelParameters(temperature=0.7, max_tokens=8192, top_p=0.9),
        ),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    body = captured["json"]
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 8192
    assert body["top_p"] == 0.9


@pytest.mark.asyncio
async def test_model_client_captures_reasoning_content(monkeypatch):
    """chat-completions 响应的 reasoning_content 必须进入 ModelResponse.reasoning。"""

    monkeypatch.setenv("MODEL_API_KEY", "secret-value")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "12,093",
                            "reasoning_content": "先计算 417*29=12093。",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        _model(),
        messages=[{"role": "user", "content": "test"}],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert result.reasoning == "先计算 417*29=12093。"


@pytest.mark.asyncio
async def test_model_client_reasoning_absent_is_empty(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
                ],
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        _model(),
        messages=[{"role": "user", "content": "test"}],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert not result.reasoning
