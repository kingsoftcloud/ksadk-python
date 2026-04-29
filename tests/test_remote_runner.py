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
        self.__class__.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return _FakeResponse(json_payload=self.post_payload)

    def stream(self, method, url, json=None, headers=None):
        self.__class__.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return _FakeStream(_FakeResponse(lines=self.stream_lines))


@pytest.mark.asyncio
async def test_remote_runner_responses_invoke_posts_to_responses(monkeypatch):
    import httpx

    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    runner = RemoteRunner(endpoint="https://agent.example.com", api_key="ak-demo", api_format="responses")

    payload = await runner.invoke({"input": "hi", "session_id": "sess-1"})

    assert payload == {"output": "hello responses"}
    assert _FakeAsyncClient.calls[0]["url"] == "https://agent.example.com/v1/responses"
    assert _FakeAsyncClient.calls[0]["json"] == {
        "input": [{"role": "user", "content": "hi"}],
        "stream": False,
        "session_id": "sess-1",
    }
    assert _FakeAsyncClient.calls[0]["headers"]["Authorization"] == "Bearer ak-demo"


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
