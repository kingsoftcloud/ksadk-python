"""A2ASpaceClient 动态发现 + egress + 调用 测试 (goal-06,-k discovery)。

- mock SpaceDiscoveryBackend 提供 hosted/external Agent(§5.5 统一模型)。
- hosted 调用经 goal-05 A2AProtocolServer(ASGI in-process)roundtrip。
- external 调用受 egress 约束:关→``A2A_SPACE_REQUIRES_PUBLIC_EGRESS``;开→通。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from ksadk.a2a import (
    A2AConfig,
    A2ARuntimeTaskAdapter,
    A2ASpaceClient,
    DiscoveredAgent,
    SpaceAgentPage,
    SpaceDiscoveryBackend,
    add_a2a_protocol_routes,
    build_agent_card,
)
from ksadk.a2a.space_client import (
    ENV_A2A_ENABLE_PUBLIC_EGRESS,
    ENV_A2A_SERVICE_URL,
    ENV_A2A_SPACE_ID,
    ERR_REQUIRES_PUBLIC_EGRESS,
)
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


class _EchoRunner:
    async def invoke(self, input_data):
        return {"output": f"echo:{input_data['input']}"}

    async def stream(self, input_data):
        yield {"delta": "echo:", "type": "text"}
        yield {"delta": str(input_data["input"]), "type": "text"}
        yield {"output": f"echo:{input_data['input']}", "type": "final"}


def _echo_app(dsn: str) -> FastAPI:
    app = FastAPI()
    runner = _EchoRunner()
    add_a2a_protocol_routes(
        app,
        runner,
        A2AConfig(
            enabled=True,
            base_url="http://testserver",
            agent_name="echo-agent",
            skills=["echo"],
            task_store_dsn=dsn,
            create_table=True,
        ),
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner, runtime_type="test"), runtime_type="test"
        ),
    )
    return app


def _agent(agent_id: str, source: str, name: str = "echo-agent") -> DiscoveredAgent:
    return DiscoveredAgent(
        agent_id=agent_id,
        version_id=f"{agent_id}-v1",
        source=source,
        agent_card=build_agent_card(name=name, base_url="http://testserver", skills=["echo"]),
        etag="etag-1",
    )


class _MockDiscoveryBackend(SpaceDiscoveryBackend):
    def __init__(self, agents):
        self._agents = list(agents)
        self.calls: list[dict] = []

    async def list_space_agents(self, space_id, *, prompt=None, skill=None, **kwargs):
        self.calls.append({"space_id": space_id, "prompt": prompt, "skill": skill})
        # §5.5:7 月 Prompt 仅做名称/描述/skills 受控关键词匹配;mock 简单过滤。
        agents = self._agents
        if skill:
            agents = [a for a in agents if any(s.id == skill for s in a.agent_card.skills)]
        return SpaceAgentPage(agents=agents, etag="etag-1")


def _client_for_app(app: FastAPI, agents, *, egress: bool) -> A2ASpaceClient:
    """构造 SpaceClient,其 httpx_client 指到 echo app(经 ASGI)。"""
    transport = httpx.ASGITransport(app=app)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return A2ASpaceClient(
        "as-test",
        _MockDiscoveryBackend(agents),
        egress_enabled=egress,
        httpx_client=httpx_client,
    )


def test_from_env_requires_space_id(monkeypatch):
    monkeypatch.delenv(ENV_A2A_SPACE_ID, raising=False)
    with pytest.raises(ValueError, match=ENV_A2A_SPACE_ID):
        A2ASpaceClient.from_env()


def test_from_env_builds_with_kop_backend(monkeypatch):
    monkeypatch.setenv(ENV_A2A_SPACE_ID, "as-1")
    monkeypatch.setenv(ENV_A2A_SERVICE_URL, "http://kop")
    monkeypatch.setenv(ENV_A2A_ENABLE_PUBLIC_EGRESS, "true")
    client = A2ASpaceClient.from_env()
    assert client._space_id == "as-1"
    assert client._egress_enabled is True


@pytest.mark.asyncio
async def test_discovery_returns_unified_hosted_external(tmp_path):
    agents = [_agent("ar-1", "hosted"), _agent("aa-1", "external")]
    client = _client_for_app(_echo_app(f"sqlite+aiosqlite:///{tmp_path}/t.db"), agents, egress=True)
    discovered = await client.discover()
    assert {a.agent_id for a in discovered} == {"ar-1", "aa-1"}
    assert {a.agent_id: a.source for a in discovered} == {"ar-1": "hosted", "aa-1": "external"}
    # prompt/skill 透传到 backend(§5.5 受控关键词匹配由服务端做)
    await client.discover(prompt="天气", skill="echo")
    assert client._backend.calls[-1] == {"space_id": "as-test", "prompt": "天气", "skill": "echo"}


@pytest.mark.asyncio
async def test_send_message_to_hosted_via_discovery(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _client_for_app(app, [_agent("ar-1", "hosted")], egress=False)
    await client.discover()
    task = await client.send_message("ar-1", "ping", return_immediately=True)
    assert task is not None and task.id


@pytest.mark.asyncio
async def test_external_blocked_when_egress_disabled(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _client_for_app(app, [_agent("aa-1", "external")], egress=False)
    await client.discover()
    with pytest.raises(PermissionError, match=ERR_REQUIRES_PUBLIC_EGRESS):
        await client.send_message("aa-1", "ping")


@pytest.mark.asyncio
async def test_external_allowed_when_egress_enabled(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _client_for_app(app, [_agent("aa-1", "external")], egress=True)
    await client.discover()
    task = await client.send_message("aa-1", "ping", return_immediately=True)
    assert task is not None and task.id
