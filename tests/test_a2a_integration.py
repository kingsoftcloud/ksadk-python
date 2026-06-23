from __future__ import annotations

import httpx
import pytest
from sse_starlette.sse import AppStatus

from ksadk.a2a import AgentCardBuilder, KsA2AServer, RemoteA2AAgent, RemoteA2AClient, to_a2a


@pytest.fixture(autouse=True)
def _reset_sse_app_status():
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None


class _InvokeOnlyRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def invoke(self, input_data):
        self.calls.append(input_data)
        return {"output": f"invoke:{input_data['input']}"}


class _StreamingRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def invoke(self, input_data):
        self.calls.append({"mode": "invoke", **input_data})
        return {"output": f"invoke:{input_data['input']}"}

    async def stream(self, input_data):
        self.calls.append(input_data)
        yield {"delta": "hello", "type": "text"}
        yield {"delta": " world", "type": "text"}


class _StreamingRunnerWithFinalChunk(_StreamingRunner):
    async def stream(self, input_data):
        self.calls.append(input_data)
        yield {"delta": "hello", "type": "text"}
        yield {"delta": " world", "type": "text"}
        yield {"output": "hello world", "type": "final"}


class _StreamingRunnerWithOverrideFinalChunk(_StreamingRunner):
    async def stream(self, input_data):
        self.calls.append(input_data)
        yield {"delta": "hello", "type": "text"}
        yield {"delta": " world", "type": "text"}
        yield {"output": "goodbye", "type": "final"}


class _FailingRunner:
    async def invoke(self, input_data):
        raise RuntimeError(f"boom:{input_data['input']}")


def test_agent_card_builder_defaults():
    card = AgentCardBuilder(
        name="demo",
        url="http://localhost:8000",
        skills=["search"],
    ).build()

    assert card.name == "demo"
    assert card.url == "http://localhost:8000"
    assert card.capabilities.streaming is True
    assert card.capabilities.push_notifications is False
    assert card.default_input_modes == ["text/plain"]
    assert card.default_output_modes == ["text/plain"]
    assert card.skills[0].id == "search"
    assert card.skills[0].tags == ["search"]


@pytest.mark.asyncio
async def test_a2a_server_exposes_cards_and_supports_invoke_roundtrip():
    runner = _InvokeOnlyRunner()
    server = to_a2a(
        runner=runner,
        app_name="echo_agent",
        url="http://testserver",
        description="Echo test agent",
        skills=["echo"],
    )
    app = server.build()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        current_card = await http_client.get("/.well-known/agent-card.json")
        legacy_card = await http_client.get("/.well-known/agent.json")

        assert current_card.status_code == 200
        assert legacy_card.status_code == 200
        assert current_card.json()["name"] == "echo_agent"
        assert legacy_card.json()["name"] == "echo_agent"

        client = RemoteA2AClient(endpoint="http://testserver", http_client=http_client)
        card = await client.get_card()
        result = await client.invoke("ping", context_id="session-1")

    assert card.name == "echo_agent"
    assert result["output"] == "invoke:ping"
    assert result["context_id"] == "session-1"
    assert runner.calls == [
        {
            "input": "ping",
            "task_id": result["task_id"],
            "context_id": "session-1",
            "session_id": "session-1",
            "state": {},
            "branch": "",
            "metadata": {},
        }
    ]


@pytest.mark.asyncio
async def test_remote_a2a_agent_streams_chunks_and_adapts_runner_contract():
    runner = _StreamingRunner()
    server = KsA2AServer(
        runner=runner,
        app_name="stream_agent",
        url="http://testserver",
        skills=["stream"],
    )
    app = server.build()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        agent = RemoteA2AAgent(
            endpoint="http://testserver",
            name="remote_stream_agent",
            http_client=http_client,
        )

        invoke_result = await agent.invoke(
            {
                "input": "hi",
                "session_id": "thread-1",
                "state": {"topic": "streaming"},
                "branch": "fanout-a",
            }
        )
        chunks = [
            chunk
            async for chunk in agent.stream(
                {
                    "input": "hi",
                    "session_id": "thread-1",
                    "state": {"topic": "streaming"},
                    "branch": "fanout-a",
                }
            )
        ]

    assert invoke_result["output"] == "hello world"
    assert invoke_result["context_id"] == "thread-1"
    assert [chunk["delta"] for chunk in chunks] == ["hello", " world"]
    assert all(chunk["context_id"] == "thread-1" for chunk in chunks)
    assert runner.calls == [
        {
            "input": "hi",
            "task_id": invoke_result["task_id"],
            "context_id": "thread-1",
            "session_id": "thread-1",
            "state": {"topic": "streaming"},
            "branch": "fanout-a",
            "metadata": {
                "state": {"topic": "streaming"},
                "branch": "fanout-a",
            },
        },
        {
            "input": "hi",
            "task_id": chunks[0]["task_id"],
            "context_id": "thread-1",
            "session_id": "thread-1",
            "state": {"topic": "streaming"},
            "branch": "fanout-a",
            "metadata": {
                "state": {"topic": "streaming"},
                "branch": "fanout-a",
            },
        },
    ]


@pytest.mark.asyncio
async def test_remote_a2a_client_ignores_duplicate_final_stream_output():
    server = KsA2AServer(
        runner=_StreamingRunnerWithFinalChunk(),
        app_name="stream_agent",
        url="http://testserver",
        skills=["stream"],
    )
    app = server.build()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = RemoteA2AClient(endpoint="http://testserver", http_client=http_client)
        result = await client.invoke("hi", context_id="thread-1")

    assert result["output"] == "hello world"


@pytest.mark.asyncio
async def test_remote_a2a_client_uses_non_prefix_final_stream_output_as_authoritative():
    server = KsA2AServer(
        runner=_StreamingRunnerWithOverrideFinalChunk(),
        app_name="stream_agent",
        url="http://testserver",
        skills=["stream"],
    )
    app = server.build()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = RemoteA2AClient(endpoint="http://testserver", http_client=http_client)
        result = await client.invoke("hi", context_id="thread-1")

    assert result["output"] == "goodbye"


@pytest.mark.asyncio
async def test_remote_a2a_client_raises_for_failed_tasks():
    server = KsA2AServer(
        runner=_FailingRunner(),
        app_name="broken_agent",
        url="http://testserver",
    )
    app = server.build()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = RemoteA2AClient(endpoint="http://testserver", http_client=http_client)
        with pytest.raises(RuntimeError, match="boom:oops"):
            await client.invoke("oops")
