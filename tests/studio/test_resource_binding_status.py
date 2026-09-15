import httpx

from ksadk.resource_runtime.supervisor import ResourceSupervisor
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import AgentBindings, AgentSpec
from ksadk.studio.service import StudioService
from tests.resource_runtime.test_studio_resource_config import binding
from tests.resource_runtime.test_worker_process import initialization


async def test_status_observes_exact_live_activation_and_revocation(tmp_path):
    studio = StudioService(tmp_path)
    for agent_id in ("agent-a", "agent-b"):
        studio.drafts.create(
            agent_id=agent_id,
            name=agent_id,
            spec=AgentSpec(bindings=AgentBindings(plugins=[binding()])),
        )
    supervisor = ResourceSupervisor("generation-a")
    studio.dsh_capabilities._resource_supervisor = supervisor
    init = initialization("https://resources.example.test")
    active = await supervisor.activate(init)
    app = create_studio_app(
        tmp_path,
        service=studio,
        session_token="fixture-session",
        csrf_token="fixture-csrf",
    )
    url = "/api/v1/agents/agent-a/resource-bindings/status"
    headers = {"X-AgentKit-Session": "fixture-session"}
    params = {"activationId": active.activation_id}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            assert (await client.get(url, params=params)).status_code == 401
            result = await client.get(url, params=params, headers=headers)
            assert result.status_code == 200
            body = result.json()
            assert body["buildDigest"] == init.scopes[0].build_digest
            assert body["bindingSnapshotDigest"] == init.resource_snapshot.digest
            assert body["items"][0]["runtimeState"] == "available"
            assert body["upstreamVerification"] == "not-observed"
            assert "revision" not in body
            for hidden in (
                active.leases[0].handle,
                str(active.socket_path),
                "fake-selected-secret",
                "account-a",
                "user-a",
            ):
                assert hidden not in result.text
            assert (
                await client.get(url.replace("agent-a", "agent-b"), params=params, headers=headers)
            ).status_code == 404
            # An expired lease is unavailable even while the Worker is alive.
            supervisor._registry._clock = lambda: float("inf")
            expired = (await client.get(url, params=params, headers=headers)).json()
            assert expired["items"][0]["runtimeState"] == "unavailable"
            assert expired["items"][0]["allowedOperations"] == []
            await studio.dsh_capabilities.deactivate_resources(active.activation_id)
            assert (await client.get(url, params=params, headers=headers)).status_code == 404
            # Reading status never creates the otherwise unused Core.
            assert studio.dsh_capabilities._host is None
    finally:
        await supervisor.aclose()


async def test_status_without_activation_does_not_use_draft_as_ready(tmp_path):
    studio = StudioService(tmp_path)
    studio.drafts.create(agent_id="agent-a", name="A", spec=AgentSpec())
    assert (
        await studio.dsh_capabilities.resource_runtime_status(
            agent_id="agent-a",
            activation_id="missing",
        )
        is None
    )
    assert studio.dsh_capabilities._host is None
