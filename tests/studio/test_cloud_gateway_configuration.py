"""Studio 的签名客户端只使用显式配置，不猜测私有入口。"""

from pathlib import Path

import pytest

from ksadk.api import AgentEngineAPIError
from ksadk.studio.cloud import DirectAgentEngineCloudDeploymentGateway
from ksadk.studio.configuration import WorkspaceConfiguration
from ksadk.studio.contracts import DeploymentRequest, DeploymentTarget
from ksadk.studio.workspace import Workspace


def _composition(tmp_path: Path):
    from ksadk.studio.service import StudioService

    service = StudioService.__new__(StudioService)
    service.configuration = WorkspaceConfiguration(Workspace(tmp_path))
    return service


def _environment(monkeypatch, *, physical_region=False, stream=True):
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "fixture-access")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "fixture-secret")
    monkeypatch.setenv("KSYUN_REGION", "pre-online")
    monkeypatch.setenv("AGENTENGINE_SERVER_URL", "https://control.example.invalid")
    if physical_region:
        monkeypatch.setenv("AGENTENGINE_REGION", "cn-beijing-6")
    else:
        monkeypatch.delenv("AGENTENGINE_REGION", raising=False)
    if stream:
        monkeypatch.setenv("AGENTENGINE_STREAM_SERVER_URL", "https://stream.example.invalid")
    else:
        monkeypatch.delenv("AGENTENGINE_STREAM_SERVER_URL", raising=False)


@pytest.mark.parametrize("physical_region", [False, True])
def test_explicit_stream_endpoint_has_its_own_signed_client(
    tmp_path, monkeypatch, physical_region,
):
    captured = []

    class Client:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    _environment(monkeypatch, physical_region=physical_region)
    monkeypatch.setattr("ksadk.studio.service.AgentEngineClient", Client)
    gateway = _composition(tmp_path)._configured_cloud_gateway()
    assert isinstance(gateway, DirectAgentEngineCloudDeploymentGateway)
    assert captured == [
        {"base_url": endpoint, "region": "pre-online",
         "access_key": "fixture-access", "secret_key": "fixture-secret"}
        for endpoint in ("https://control.example.invalid", "https://stream.example.invalid")
    ]
    assert gateway.stream_client is not gateway.client
    assert gateway._managed_runtime_client() is gateway.stream_client


def test_missing_stream_endpoint_does_not_invent_a_fallback(tmp_path, monkeypatch):
    captured = []

    class Client:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    _environment(monkeypatch, stream=False)
    monkeypatch.setattr("ksadk.studio.service.AgentEngineClient", Client)
    gateway = _composition(tmp_path)._configured_cloud_gateway()
    assert len(captured) == 1
    assert captured[0]["base_url"] == "https://control.example.invalid"
    assert gateway.stream_client is gateway.client
    assert gateway._managed_runtime_client() is gateway.client


async def test_control_failure_does_not_try_an_implicit_private_fallback(tmp_path, monkeypatch):
    constructed, calls = [], []
    failure = AgentEngineAPIError(503, "fixture unavailable")

    class Client:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        async def create_agent(self, payload):
            calls.append(payload)
            raise failure

    _environment(monkeypatch, stream=False)
    monkeypatch.setattr("ksadk.studio.service.AgentEngineClient", Client)
    gateway = _composition(tmp_path)._configured_cloud_gateway()
    with pytest.raises(AgentEngineAPIError) as caught:
        await gateway.create_managed_runtime_deployment(
            build_id="build-fixture", agent_name="fixture-agent",
            manifest="name: fixture-agent\nframework: codex\n", manifest_digest="a" * 64,
            runtime_name="codex", runtime_version="0.147.0",
            request=DeploymentRequest(target=DeploymentTarget(
                region="pre-online", environment="preproduction",
            )),
        )
    assert caught.value is failure
    assert len(constructed) == len(calls) == 1
