from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runtime.launch import RuntimeLaunchContext
from ksadk.server.factory import RuntimeAppConfig
from ksadk.tunnel.config import (
    default_tunnel_self_url,
    prepare_tunnel_context,
    tunnel_self_url_for_start,
)


def _detection(runtime_type: str = "adk", name: str = "demo_agent"):
    return SimpleNamespace(
        name=name,
        type=SimpleNamespace(value=runtime_type),
    )


def _clear_tunnel_env(monkeypatch) -> None:
    for name in (
        "KSADK_ENABLE_TUNNEL",
        "KSADK_TUNNEL_TOKEN",
        "KSADK_TUNNEL_SELF_URL",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_tunnel_disabled_preserves_launch_context(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_TUNNEL_SELF_URL", "not a valid URL")

    context = RuntimeLaunchContext(
        runtime_type="adk",
        project_dir=tmp_path,
        detection=_detection(),
    )
    app_config = RuntimeAppConfig(
        launch_context=context,
        tunnel_self_url=tunnel_self_url_for_start(18080),
    )

    assert context.services.tunnel_registry is None
    assert app_config.launch_context is context
    assert app_config.tunnel is None


def test_tunnel_enabled_requires_token(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")

    with pytest.raises(ValueError, match="KSADK_TUNNEL_TOKEN"):
        RuntimeLaunchContext(
            runtime_type="adk",
            project_dir=tmp_path,
            detection=_detection(),
        )


def test_tunnel_enabled_rejects_non_adk_runtime(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-tunnel-token")

    with pytest.raises(ValueError, match="requires the ADK runtime"):
        RuntimeLaunchContext(
            runtime_type="langgraph",
            project_dir=tmp_path,
            detection=_detection("langgraph"),
        )


def test_tunnel_registry_is_shared_and_start_port_refines_same_instance(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "true")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-tunnel-token")

    early_context = RuntimeLaunchContext(
        runtime_type="adk",
        project_dir=tmp_path,
        detection=_detection(),
    )
    registry = early_context.services.tunnel_registry
    assert registry is not None
    assert registry.self_url == "http://127.0.0.1:8000"

    prepared_context, config = prepare_tunnel_context(
        early_context,
        self_url="http://127.0.0.1:18080",
    )

    assert prepared_context.services.tunnel_registry is registry
    assert registry.self_url == "http://127.0.0.1:18080"
    assert config is not None
    assert config.agent_name == "demo_agent"
    assert config.self_url == registry.self_url
    assert "fake-tunnel-token" not in repr(config)


def test_tunnel_self_url_explicit_override_and_loopback_validation(monkeypatch):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_TUNNEL_SELF_URL", "http://127.0.0.1:19090/")
    assert default_tunnel_self_url(8080) == "http://127.0.0.1:19090"

    monkeypatch.setenv("KSADK_TUNNEL_SELF_URL", "https://example.com")
    with pytest.raises(ValueError, match="loopback"):
        default_tunnel_self_url(8080)


@pytest.mark.asyncio
async def test_app_owned_toolset_survives_overlapping_and_sequential_runners(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-tunnel-token")

    from ksadk.runners.adk_runner import ADKRunner
    from ksadk.tunnel.config import tunnel_config_for_detection
    from ksadk.tunnel.registry import TunnelRegistry

    detection = _detection()
    config = tunnel_config_for_detection(detection)
    root_agent = SimpleNamespace(tools=[])
    registry = TunnelRegistry()
    registry.self_url = "http://127.0.0.1:18080"

    def make_runner(current_registry):
        runner = ADKRunner(detection, str(tmp_path))
        runner._agent = root_agent
        runner._tunnel_config = config
        runner._tunnel_registry = current_registry
        return runner

    first = make_runner(registry)
    second = make_runner(registry)
    first._inject_tunnel_toolset()
    second._inject_tunnel_toolset()
    shared = root_agent.tools[0]

    assert len(root_agent.tools) == 1
    assert first._runtime_toolsets == []
    assert second._runtime_toolsets == []

    # Closing one overlapping runner must not close the other runner's Toolset.
    await first.close()
    assert shared.closed is False
    second._inject_tunnel_toolset()
    assert len(root_agent.tools) == 1
    await second.close()
    assert shared.closed is False

    # A later runner reuses the same cached root_agent and app-owned Toolset.
    third = make_runner(registry)
    third._inject_tunnel_toolset()
    assert root_agent.tools == [shared]
    await third.close()
    assert shared.closed is False

    await registry.close()
    await registry.close()
    assert shared.closed is True

    # A new app in the same interpreter detaches only the already-closed Toolset.
    next_registry = TunnelRegistry()
    next_registry.self_url = registry.self_url
    fourth = make_runner(next_registry)
    fourth._inject_tunnel_toolset()
    assert len(root_agent.tools) == 1
    assert root_agent.tools[0] is not shared
    assert root_agent.tools[0].registry is next_registry
    await fourth.close()
    await next_registry.close()


@pytest.mark.asyncio
async def test_manual_toolset_with_different_registry_fails_closed(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-tunnel-token")

    from ksadk.runners.adk_runner import ADKRunner
    from ksadk.tunnel import TunnelToolset
    from ksadk.tunnel.config import tunnel_config_for_detection
    from ksadk.tunnel.registry import TunnelRegistry

    detection = _detection()
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(tools=[TunnelToolset("demo_agent")])
    runner._tunnel_config = tunnel_config_for_detection(detection)
    runner._tunnel_registry = TunnelRegistry()

    with pytest.raises(ValueError, match="must not be mixed"):
        runner._inject_tunnel_toolset()

    await runner.close()
    await runner._tunnel_registry.close()


@pytest.mark.asyncio
async def test_runner_owned_fallback_registry_closes_and_recreates(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-tunnel-token")

    from ksadk.runners.adk_runner import ADKRunner

    detection = _detection()
    root_agent = SimpleNamespace(tools=[])
    first = ADKRunner(detection, str(tmp_path))
    first._agent = root_agent
    first._inject_tunnel_toolset()
    first_registry = first._tunnel_registry
    first_toolset = root_agent.tools[0]
    assert first._owns_tunnel_registry is True

    await first.close()
    assert first_toolset.closed is True

    second = ADKRunner(detection, str(tmp_path))
    second._agent = root_agent
    second._inject_tunnel_toolset()
    assert len(root_agent.tools) == 1
    assert root_agent.tools[0] is not first_toolset
    assert second._tunnel_registry is not first_registry

    await second.close()
    assert root_agent.tools[0].closed is True


@pytest.mark.asyncio
async def test_early_adapter_shares_registry_with_later_app_port(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-tunnel-token")

    from ksadk.runners.adk_runner import ADKRunner
    from ksadk.runtime.factory import build_default_runtime_registry
    from ksadk.runtime.launch import RuntimeServices

    detection = _detection()
    runner = ADKRunner(detection, str(tmp_path))
    context = RuntimeLaunchContext(
        runtime_type="adk",
        project_dir=tmp_path,
        detection=detection,
        services=RuntimeServices(runner_factory=lambda *_: runner),
    )
    build_default_runtime_registry().create(context)
    registry = context.services.tunnel_registry
    assert runner._tunnel_registry is registry

    prepare_tunnel_context(context, self_url="http://127.0.0.1:18080")
    runner._agent = SimpleNamespace(tools=[])
    runner._inject_tunnel_toolset()
    toolset = runner._agent.tools[0]

    assert toolset._registry is registry
    assert toolset._self_base_url() == "http://127.0.0.1:18080"
    await runner.close()
    await registry.close()


def test_runtime_web_app_uses_actual_cli_port(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-tunnel-token")
    monkeypatch.delenv("AGENTENGINE_MANAGED_RUNTIME", raising=False)
    monkeypatch.delenv("KSADK_A2A_RUNTIME_ID", raising=False)

    from ksadk.cli.runtime_bootstrap import create_runtime_web_app
    from ksadk.detection.detector import DetectionResult, FrameworkType

    detection = DetectionResult(
        type=FrameworkType.ADK,
        name="demo_agent",
        entry_point="agent.py",
        package_path=str(tmp_path),
    )
    app = create_runtime_web_app(detection, tmp_path, port=18080)
    state = app.state.runtime

    assert state.tunnel.self_url == "http://127.0.0.1:18080"
    assert state.launch_context.services.tunnel_registry.self_url == state.tunnel.self_url


def test_tunnel_routes_precede_hosted_ui_catchall(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-tunnel-token")

    from ksadk.server.composition import configure_runtime_app
    from ksadk.server.factory import create_runtime_app

    context = RuntimeLaunchContext(
        runtime_type="adk",
        project_dir=tmp_path,
        detection=_detection(),
    )
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"health_meta"},
            launch_context=context,
            tunnel_self_url="http://127.0.0.1:18080",
        ),
        configure_runtime_app,
    )

    paths = [route.path for route in app.routes]
    assert "/tunnel/connect" in paths
    assert "/tunnel/servers" in paths
    assert paths.index("/tunnel/servers") < paths.index("/{requested_path:path}")


def test_code_builder_tunnel_enables_mcp_runtime_requirements(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")

    from ksadk.builders.code_builder import CodeBuilder

    builder = CodeBuilder(tmp_path)
    assert builder._mcp_runtime_enabled() is True
    assert any(
        requirement.startswith("mcp") for requirement in builder._bundled_runtime_requirements()
    )


def test_code_builder_detects_embedded_tunnel_import(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    (tmp_path / "agent.py").write_text(
        "from ksadk.tunnel import TunnelToolset\n",
        encoding="utf-8",
    )

    from ksadk.builders.code_builder import CodeBuilder

    builder = CodeBuilder(tmp_path)
    assert builder._mcp_runtime_enabled() is True


def test_deployment_manager_adds_tunnel_mcp_requirement(monkeypatch, tmp_path):
    _clear_tunnel_env(monkeypatch)
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")

    from ksadk.deployment.manager import K8sDeployer

    requirements = K8sDeployer()._generate_requirements(_detection())
    assert "mcp>=1.26.0,<2.0.0" in requirements
