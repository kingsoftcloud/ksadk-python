"""Automatic runtime peer credentials without eager agent imports."""

import hashlib
import hmac
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ksadk import Agent
from ksadk.runtime.launch import RuntimeLaunchContext
from ksadk.tunnel import LocalMCPService, TunnelConnector
from ksadk.tunnel.discovery import discover_tools
from ksadk.tunnel.peers import PeerConfig
from ksadk.tunnel.runtime import DeferredTunnel
from tests.test_mcp_tunnel import connected, local_mcp, serve


@pytest.fixture(autouse=True)
def clean_peer_environment(monkeypatch):
    for name in (
        "KSADK_TUNNEL_PEER_SECRET",
        "KSADK_TUNNEL_PEER_SERVICE",
        "KSADK_TUNNEL_PEER_PORT",
        "KSADK_TUNNEL_TOKEN",
        "AGENT_RUNTIME_ID",
        "CUSTOM_TUNNEL_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_derived_secret_is_stable_scoped_and_not_the_connector_token(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ID", "runtime-one")
    first = PeerConfig.from_env(tunnel_token="fixture-tunnel-token")
    second = PeerConfig.from_env(tunnel_token="fixture-tunnel-token")
    expected = hmac.new(
        b"fixture-tunnel-token", b"ksadk-tunnel-peer-v1:runtime-one", hashlib.sha256
    ).hexdigest()
    assert first.secret == second.secret == expected
    assert first.service == "runtime-one-headless"
    assert first.port == 8092
    assert "fixture-tunnel-token" not in repr(first)
    assert expected not in repr(first)
    assert PeerConfig.from_env(tunnel_token="different-token").secret != expected
    monkeypatch.setenv("AGENT_RUNTIME_ID", "runtime-two")
    assert PeerConfig.from_env(tunnel_token="fixture-tunnel-token").secret != expected


def test_optional_secret_override_and_local_behavior(monkeypatch):
    assert PeerConfig.from_env(tunnel_token="fixture-token") is None
    monkeypatch.setenv("AGENT_RUNTIME_ID", "runtime-one")
    assert PeerConfig.from_env() is None
    monkeypatch.setenv("KSADK_TUNNEL_PEER_SECRET", "s" * 32)
    monkeypatch.setenv("KSADK_TUNNEL_PEER_SERVICE", "custom-headless")
    monkeypatch.setenv("KSADK_TUNNEL_PEER_PORT", "8093")
    config = PeerConfig.from_env(tunnel_token="fixture-token")
    assert config.secret == "s" * 32
    assert config.service == "custom-headless"
    assert config.port == 8093
    monkeypatch.setenv("KSADK_TUNNEL_PEER_SECRET", "short")
    with pytest.raises(ValueError, match="32 bytes"):
        PeerConfig.from_env(tunnel_token="fixture-token")
    monkeypatch.delenv("AGENT_RUNTIME_ID")
    with pytest.raises(ValueError, match="AGENT_RUNTIME_ID"):
        PeerConfig.from_env(tunnel_token="fixture-token")


def slot(tmp_path):
    return DeferredTunnel(
        RuntimeLaunchContext(
            "adk",
            tmp_path,
            detection=SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"),
        )
    )


@pytest.mark.parametrize("wrapped", [False, True])
async def test_disabled_or_native_agent_ignores_peer_configuration(tmp_path, monkeypatch, wrapped):
    monkeypatch.setenv("AGENT_RUNTIME_ID", "runtime-one")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fixture-token")
    monkeypatch.setenv("KSADK_TUNNEL_PEER_SECRET", "invalid")
    monkeypatch.setenv("KSADK_TUNNEL_PEER_PORT", "not-a-port")
    deferred = slot(tmp_path)
    await deferred.start()
    assert deferred.peers is None
    root = SimpleNamespace()
    if wrapped:
        root = Agent(root, enable_tunnel=False, tunnel_token_env="ABSENT_TOKEN")
    assert deferred.resolve(root, SimpleNamespace(__file__=str(tmp_path / "agent.py"))) is None
    assert deferred.peers is None
    await deferred.close()


@pytest.mark.parametrize("token_env", ["KSADK_TUNNEL_TOKEN", "CUSTOM_TUNNEL_TOKEN"])
async def test_auto_keys_with_cli_apps_and_lazy_connector_owner(tmp_path, monkeypatch, token_env):
    from ksadk.cli.runtime_bootstrap import create_runtime_web_app
    from ksadk.detection import FrameworkDetector

    monkeypatch.setenv("AGENT_RUNTIME_ID", "runtime-one")
    monkeypatch.setenv(token_env, "fixture-tunnel-token")
    if token_env != "KSADK_TUNNEL_TOKEN":
        # The declaration must win over any default variable present on the pod.
        monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "wrong-default-token")
    marker = tmp_path / "imported"
    (tmp_path / "agent.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "from ksadk import Agent\n"
        "from google.adk.agents import Agent as GoogleAgent\n"
        "root_agent = Agent(GoogleAgent(name='inventory'), enable_tunnel=True, "
        f"tunnel_token_env={token_env!r})\n"
    )
    (tmp_path / "agentengine.yaml").write_text(
        "framework: adk\nentry_point: agent.py\nagent_variable: root_agent\n"
    )
    original = PeerConfig.from_env

    def local_config(cls, **kwargs):
        config = original(**kwargs)
        return replace(config, port=0, host="127.0.0.1", advertised_host="127.0.0.1")

    monkeypatch.setattr(PeerConfig, "from_env", classmethod(local_config))
    detection = FrameworkDetector(str(tmp_path)).detect()
    a = create_runtime_web_app(detection, tmp_path)
    b = create_runtime_web_app(detection, tmp_path)
    a_slot = a.state.runtime.launch_context.services.tunnel_binding
    b_slot = b.state.runtime.launch_context.services.tunnel_binding
    async with local_mcp() as (endpoint, calls, _, _), serve(a) as url, serve(b):
        assert not marker.exists()  # App lifespan never loads the user agent.
        assert a_slot.peers is b_slot.peers is None
        client = TunnelConnector(
            url,
            token="fixture-tunnel-token",
            services=[LocalMCPService("inventory", endpoint, tool_filter=("echo",))],
        )
        async with connected(client):
            assert marker.exists()
            assert a_slot.peers.server is not None  # Connector starts its owner's listener.
            binding = b_slot.for_connector()  # Normal runner load uses the same resolution path.
            await binding.start()
            assert a_slot.peers.key == b_slot.peers.key
            assert a_slot.peers.config.secret != "fixture-tunnel-token"

            async def addresses():
                return {a_slot.peers.owner["address"], b_slot.peers.owner["address"]}

            a_slot.peers._resolver = b_slot.peers._resolver = addresses
            (tool,) = await discover_tools("root", binding.registry)
            assert (await tool.call({"text": "automatic"}))["structuredContent"] == {
                "text": "automatic"
            }
            assert calls == ["automatic"]


def test_missing_custom_token_does_not_fall_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ID", "runtime-one")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fixture-default-token")
    root = Agent(SimpleNamespace(), enable_tunnel=True, tunnel_token_env="CUSTOM_TUNNEL_TOKEN")
    deferred = slot(tmp_path)
    with pytest.raises(ValueError, match="CUSTOM_TUNNEL_TOKEN"):
        deferred.resolve(root, SimpleNamespace(__file__=str(tmp_path / "agent.py")))
    assert deferred.peers is None
    assert deferred.binding is None
