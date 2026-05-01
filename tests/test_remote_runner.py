import pytest

from ksadk.runners.remote_runner import RemoteRunner


class _FakeResponse:
    def __init__(self, *, json_payload=None, lines=None):
        self._json_payload = json_payload or {}
        self._lines = lines or []

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_payload

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAsyncClient:
    calls = []
    post_payload = {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": "hello responses",
                    }
                ]
            }
        ]
    }
    stream_lines = [
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"hello"}',
        "event: response.reasoning.delta",
        'data: {"type":"response.reasoning.delta","delta":"thinking"}',
        "data: [DONE]",
    ]

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        self.__class__.calls.append(
            {"method": "POST", "url": url, "json": json, "headers": headers}
        )
        return _FakeResponse(json_payload=self.post_payload)

    def stream(self, method, url, json=None, headers=None):
        self.__class__.calls.append(
            {"method": method, "url": url, "json": json, "headers": headers}
        )
        return _FakeStream(_FakeResponse(lines=self.stream_lines))


@pytest.mark.asyncio
async def test_remote_runner_responses_invoke_posts_to_responses(monkeypatch):
    import httpx

    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    runner = RemoteRunner(
        endpoint="https://agent.example.com", api_key="ak-demo", api_format="responses"
    )

    payload = await runner.invoke({"input": "hi", "session_id": "sess-1"})

    assert payload == {"output": "hello responses"}
    assert _FakeAsyncClient.calls[0]["url"] == "https://agent.example.com/v1/responses"
    assert _FakeAsyncClient.calls[0]["json"] == {
        "input": "hi",
        "stream": False,
        "session_id": "sess-1",
    }
    assert _FakeAsyncClient.calls[0]["headers"]["Authorization"] == "Bearer ak-demo"


@pytest.mark.asyncio
async def test_remote_runner_responses_normalizes_chat_style_input_for_openclaw(monkeypatch):
    import httpx

    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    runner = RemoteRunner(endpoint="https://agent.example.com", api_format="responses")

    await runner.invoke(
        {
            "input": [
                {"role": "system", "content": "You are concise."},
                {"role": "user", "content": [{"type": "input_text", "text": "你好"}]},
            ]
        }
    )

    assert _FakeAsyncClient.calls[0]["json"]["input"] == "你好"


@pytest.mark.asyncio
async def test_remote_runner_responses_keeps_standard_item_array_and_previous_response_id(
    monkeypatch,
):
    import httpx

    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    runner = RemoteRunner(endpoint="https://agent.example.com", api_format="responses")

    await runner.invoke(
        {
            "input": {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": "ok",
            },
            "previous_response_id": "resp_123",
        }
    )

    assert _FakeAsyncClient.calls[0]["json"]["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": "ok",
        }
    ]
    assert _FakeAsyncClient.calls[0]["json"]["previous_response_id"] == "resp_123"


@pytest.mark.asyncio
async def test_remote_runner_responses_stream_parses_text_and_reasoning(monkeypatch):
    import httpx

    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    runner = RemoteRunner(endpoint="https://agent.example.com", api_format="responses")

    chunks = [chunk async for chunk in runner.stream({"input": "hi"})]

    assert _FakeAsyncClient.calls[0]["url"] == "https://agent.example.com/v1/responses"
    assert chunks == [
        {"delta": "hello", "type": "text"},
        {"delta": "thinking", "type": "thinking"},
    ]


@pytest.mark.asyncio
async def test_remote_runner_responses_stream_parses_native_tool_items(monkeypatch):
    import httpx

    class ToolStreamClient(_FakeAsyncClient):
        stream_lines = [
            "event: response.output_item.added",
            (
                'data: {"output_index":0,"item":{"id":"fc_1","type":"function_call",'
                '"name":"search","arguments":""}}'
            ),
            "",
            "event: response.function_call_arguments.delta",
            'data: {"item_id":"fc_1","delta":"{\\"q\\":"}',
            "",
            "event: response.function_call_arguments.delta",
            'data: {"item_id":"fc_1","delta":"\\"openclaw\\"}"}',
            "",
            "event: response.function_call_arguments.done",
            'data: {"item_id":"fc_1","arguments":"{\\"q\\":\\"openclaw\\"}"}',
            "",
            "event: response.output_item.done",
            (
                'data: {"output_index":0,"item":{"id":"out_1",'
                '"type":"function_call_output","call_id":"fc_1","output":{"ok":true}}}'
            ),
            "",
            "event: response.completed",
            (
                'data: {"response":{"id":"resp_1","output":[{"id":"fc_1",'
                '"type":"function_call","name":"search",'
                '"arguments":"{\\"q\\":\\"openclaw\\"}"},{"id":"out_1",'
                '"type":"function_call_output","call_id":"fc_1","output":{"ok":true}}]}}'
            ),
            "",
            "data: [DONE]",
        ]

    ToolStreamClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", ToolStreamClient)
    runner = RemoteRunner(endpoint="https://agent.example.com", api_format="responses")

    chunks = [chunk async for chunk in runner.stream({"input": "hi"})]

    assert chunks == [
        {"type": "tool_call", "tool_name": "search", "tool_args": "", "status": "running"},
        {"type": "tool_call", "tool_name": "search", "tool_args": '{"q":', "status": "running"},
        {
            "type": "tool_call",
            "tool_name": "search",
            "tool_args": '{"q":"openclaw"}',
            "status": "running",
        },
        {
            "type": "tool_call",
            "tool_name": "search",
            "tool_args": '{"q":"openclaw"}',
            "status": "running",
        },
        {"type": "tool_result", "tool_name": "search", "tool_output": '{\n  "ok": true\n}'},
        {
            "type": "responses_output",
            "output": [
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "name": "search",
                    "arguments": '{"q":"openclaw"}',
                },
                {
                    "id": "out_1",
                    "type": "function_call_output",
                    "call_id": "fc_1",
                    "output": {"ok": True},
                },
            ],
            "response_id": "resp_1",
        },
    ]


@pytest.mark.asyncio
async def test_remote_runner_responses_can_send_openclaw_session_header(monkeypatch):
    import httpx

    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    runner = RemoteRunner(
        endpoint="https://agent.example.com",
        api_key="gateway-token",
        api_format="responses",
        responses_session_header="x-openclaw-session-key",
    )

    await runner.invoke({"input": "hi", "session_id": "sess-1"})

    assert _FakeAsyncClient.calls[0]["headers"]["Authorization"] == "Bearer gateway-token"
    assert _FakeAsyncClient.calls[0]["headers"]["x-openclaw-session-key"] == "sess-1"
    assert "session_id" not in _FakeAsyncClient.calls[0]["json"]


@pytest.mark.asyncio
async def test_remote_runner_responses_stream_surfaces_failed_event(monkeypatch):
    import httpx

    class FailedStreamClient(_FakeAsyncClient):
        stream_lines = [
            "event: response.failed",
            'data: {"response":{"error":{"code":"api_error","message":"internal error"}}}',
            "",
            "data: [DONE]",
        ]

    FailedStreamClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FailedStreamClient)
    runner = RemoteRunner(endpoint="https://agent.example.com", api_format="responses")

    chunks = [chunk async for chunk in runner.stream({"input": "hi"})]

    assert chunks == [{"type": "error", "message": "internal error"}]
