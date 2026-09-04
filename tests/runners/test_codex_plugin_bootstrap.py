from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.codex.client import (
    AsyncCodexClient,
    CodexClient,
    CodexPluginBootstrap,
    CodexPluginBootstrapError,
    codex_marketplace_tree_digest,
)
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runtime.adapter import StartRequest
from ksadk.runtime.factory import (
    _codex_plugin_bootstrap,
    _isolated_codex_home,
    create_runtime_adapter,
)
from ksadk.runtime.launch import RuntimeLaunchContext, RuntimeServices
from ksadk.studio.codex_plugin_store import _tree_digest as stored_marketplace_digest


def _write_marketplace(
    root: Path,
    *,
    marketplace_name: str = "ksadk-build-a1b2c3d4",
    plugin_names: tuple[str, ...] = ("alpha", "zeta"),
) -> CodexPluginBootstrap:
    manifest = root / ".agents" / "plugins" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": marketplace_name,
                "plugins": [
                    {
                        "name": name,
                        "source": {"source": "local", "path": f"./plugins/{name}"},
                    }
                    for name in plugin_names
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    for name in plugin_names:
        plugin_manifest = root / "plugins" / name / ".codex-plugin" / "plugin.json"
        plugin_manifest.parent.mkdir(parents=True)
        plugin_manifest.write_text(json.dumps({"name": name}), encoding="utf-8")
    return CodexPluginBootstrap(
        marketplace_path=str(root),
        marketplace_name=marketplace_name,
        plugin_names=plugin_names,
        snapshot_digest=codex_marketplace_tree_digest(root),
    )


class _BootstrapClient(CodexClient):
    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.bootstrap_calls: list[CodexPluginBootstrap] = []
        self.events: list[str] = []
        self.thread_count = 0

    async def bootstrap_plugins(self, config: CodexPluginBootstrap) -> None:
        self.events.append("bootstrap")
        self.bootstrap_calls.append(config)
        await asyncio.sleep(0)
        if self.fail_first and len(self.bootstrap_calls) == 1:
            raise CodexPluginBootstrapError("fixture bootstrap failure")

    async def start_thread(self, config: dict[str, Any] | None = None) -> str:
        del config
        self.events.append("start_thread")
        self.thread_count += 1
        return f"thread-{self.thread_count}"

    def run_turn(self, thread_id: str, prompt: Any, *, config=None):
        del thread_id, prompt, config

        async def _events():
            if False:  # pragma: no cover - keeps this an async generator
                yield {}

        return _events()

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        del thread_id
        return False

    async def resume_thread(self, thread_id: str, config=None) -> str:
        del config
        return thread_id

    async def close(self) -> None:
        return None


def _start_request(index: int = 1) -> StartRequest:
    return StartRequest(input="go", user_id="u", session_id=f"s-{index}")


@pytest.mark.asyncio
async def test_runtime_bootstraps_once_before_any_thread(tmp_path: Path) -> None:
    bootstrap = _write_marketplace(tmp_path / "marketplace")
    client = _BootstrapClient()
    adapter = CodexRuntimeAdapter(client, plugin_bootstrap=bootstrap)

    await asyncio.gather(adapter.start(_start_request(1)), adapter.start(_start_request(2)))

    assert client.bootstrap_calls == [bootstrap]
    assert client.events == ["bootstrap", "start_thread", "start_thread"]


@pytest.mark.asyncio
async def test_runtime_retries_failed_bootstrap_without_starting_thread(tmp_path: Path) -> None:
    bootstrap = _write_marketplace(tmp_path / "marketplace")
    client = _BootstrapClient(fail_first=True)
    adapter = CodexRuntimeAdapter(client, plugin_bootstrap=bootstrap)

    with pytest.raises(CodexPluginBootstrapError, match="fixture bootstrap failure"):
        await adapter.start(_start_request())
    assert client.events == ["bootstrap"]

    handle = await adapter.start(_start_request())
    assert handle.run_id == "thread-1"
    assert client.events == ["bootstrap", "bootstrap", "start_thread"]


@pytest.mark.asyncio
async def test_factory_passes_closed_bootstrap_contract_to_adapter(tmp_path: Path) -> None:
    bootstrap = _write_marketplace(tmp_path / "marketplace")
    client = _BootstrapClient()
    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=tmp_path / "project",
        config={
            "codex_home_key": "build_0123456789abcdef0123",
            "codex_plugin_bootstrap": {
                "marketplace_path": bootstrap.marketplace_path,
                "marketplace_name": bootstrap.marketplace_name,
                "plugin_names": list(bootstrap.plugin_names),
                "snapshot_digest": bootstrap.snapshot_digest,
            }
        },
        services=RuntimeServices(codex_client_factory=lambda config=None: client),
    )

    adapter = create_runtime_adapter(context)
    await adapter.start(_start_request())

    assert client.bootstrap_calls == [bootstrap]


def test_factory_requires_build_partition_for_plugin_launch(tmp_path: Path) -> None:
    bootstrap = _write_marketplace(tmp_path / "marketplace")
    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=tmp_path / "project",
        config={
            "codex_plugin_bootstrap": {
                "marketplace_path": bootstrap.marketplace_path,
                "marketplace_name": bootstrap.marketplace_name,
                "plugin_names": list(bootstrap.plugin_names),
                "snapshot_digest": bootstrap.snapshot_digest,
            }
        },
        services=RuntimeServices(codex_client_factory=_BootstrapClient),
    )

    with pytest.raises(ValueError, match="require codex_home_key"):
        create_runtime_adapter(context)


def test_codex_home_is_partitioned_by_build_for_default_and_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    first_key = "build_11111111111111111111"
    second_key = "build_22222222222222222222"

    first_default = _isolated_codex_home(project, first_key)
    second_default = _isolated_codex_home(project, second_key)
    assert first_default == project / ".agentkit" / "codex-homes" / first_key
    assert second_default == project / ".agentkit" / "codex-homes" / second_key
    (first_default / "plugin-from-first-build").write_text("installed", encoding="utf-8")
    assert not (second_default / "plugin-from-first-build").exists()
    assert not (project / ".agentkit" / "codex-home").exists()

    override = tmp_path / "configured-codex-home"
    monkeypatch.setenv("KSADK_CODEX_HOME", str(override))
    first_override = _isolated_codex_home(project, first_key)
    second_override = _isolated_codex_home(project, second_key)
    assert first_override == override / "builds" / first_key
    assert second_override == override / "builds" / second_key
    assert first_override != second_override


def test_factory_rejects_untrusted_bootstrap_extensions(tmp_path: Path) -> None:
    bootstrap = _write_marketplace(tmp_path / "marketplace")
    raw = {
        "marketplace_path": bootstrap.marketplace_path,
        "marketplace_name": bootstrap.marketplace_name,
        "plugin_names": list(bootstrap.plugin_names),
        "snapshot_digest": bootstrap.snapshot_digest,
        "hooks": [{"event": "SessionStart", "trust": "allow"}],
    }

    with pytest.raises(ValueError, match="unexpected hooks"):
        _codex_plugin_bootstrap({"codex_plugin_bootstrap": raw})


@pytest.mark.asyncio
async def test_async_client_uses_one_initialized_app_server_and_reconciles_enabled_plugins(
    tmp_path: Path,
) -> None:
    pytest.importorskip("openai_codex")
    bootstrap = _write_marketplace(tmp_path / "marketplace")
    manifest_path = str(bootstrap.manifest_path.resolve())

    class _Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any] | None, str]] = []

        async def request(self, method, params, *, response_model):
            self.calls.append((method, params, response_model.__name__))
            if method == "marketplace/add":
                return SimpleNamespace(
                    marketplace_name=bootstrap.marketplace_name,
                    installed_root=SimpleNamespace(root=bootstrap.marketplace_path),
                )
            if method == "plugin/install":
                return SimpleNamespace(auth_policy="ON_INSTALL", apps_needing_auth=[])
            if method == "plugin/list":
                return SimpleNamespace(
                    marketplaces=[
                        SimpleNamespace(
                            name=bootstrap.marketplace_name,
                            path=SimpleNamespace(root=manifest_path),
                            plugins=[
                                SimpleNamespace(name=name, installed=True, enabled=True)
                                for name in bootstrap.plugin_names
                            ],
                        )
                    ]
                )
            raise AssertionError(f"unexpected App Server method: {method}")

    class _AppServer:
        def __init__(self) -> None:
            self.initialized = 0
            self._client = _Transport()

        async def _ensure_initialized(self) -> None:
            self.initialized += 1

    app_server = _AppServer()
    client = object.__new__(AsyncCodexClient)
    client._codex = app_server
    client.sdk_version = "0.147.0"

    await client.bootstrap_plugins(bootstrap)

    assert app_server.initialized == 1
    assert [method for method, _params, _model in app_server._client.calls] == [
        "marketplace/add",
        "plugin/install",
        "plugin/install",
        "plugin/list",
    ]
    assert [
        params["pluginName"]
        for method, params, _model in app_server._client.calls
        if method == "plugin/install" and params is not None
    ] == list(bootstrap.plugin_names)
    assert all(
        params is not None and params["marketplacePath"] == manifest_path
        for method, params, _model in app_server._client.calls
        if method == "plugin/install"
    )


@pytest.mark.asyncio
async def test_async_client_rejects_digest_drift_before_app_server_initialization(
    tmp_path: Path,
) -> None:
    bootstrap = _write_marketplace(tmp_path / "marketplace")
    bootstrap.manifest_path.write_text("{}", encoding="utf-8")

    app_server = SimpleNamespace(_client=SimpleNamespace())

    async def _ensure_initialized() -> None:
        raise AssertionError("digest drift must fail before App Server initialization")

    app_server._ensure_initialized = _ensure_initialized
    client = object.__new__(AsyncCodexClient)
    client._codex = app_server
    client.sdk_version = "0.147.0"

    with pytest.raises(CodexPluginBootstrapError, match="digest mismatch"):
        await client.bootstrap_plugins(bootstrap)


def test_marketplace_digest_rejects_executable_mode_drift(tmp_path: Path) -> None:
    bootstrap = _write_marketplace(tmp_path / "marketplace")
    assert bootstrap.snapshot_digest == stored_marketplace_digest(bootstrap.root)
    plugin_manifest = bootstrap.root / "plugins" / "alpha" / ".codex-plugin" / "plugin.json"
    plugin_manifest.chmod(0o755)

    with pytest.raises(CodexPluginBootstrapError, match="digest mismatch"):
        bootstrap.verify()
