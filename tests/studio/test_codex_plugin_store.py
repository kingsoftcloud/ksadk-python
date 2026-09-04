from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ksadk.plugins.bridges.codex import (
    CodexBridgeHost,
    CodexPluginDetail,
    CodexPluginInstallResult,
    CodexPluginInventory,
    CodexPluginUninstallResult,
)
from ksadk.plugins.codex_manifest import (
    CodexPluginSourceCoordinate,
    snapshot_installed_codex_plugin,
)
from ksadk.plugins.contracts import plugin_lock_digest
from ksadk.studio import api_plugin_routes
from ksadk.studio.api_plugin_routes import (
    _codex_source_coordinate,
    register_plugin_routes,
)
from ksadk.studio.codex_builder import CodexStudioBuilder
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.codex_plugin_store import (
    CodexPluginSnapshotStore,
    find_installed_codex_plugin_root,
)
from ksadk.studio.codex_run import CodexRunSpecResolver
from ksadk.studio.contracts import NativePluginBinding
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _plugin_root(
    parent: Path,
    *,
    name: str = "fixture-plugin",
    plugin_id: str = "io.fixture.plugin",
    extension: bool = False,
) -> Path:
    root = parent / plugin_id.replace(".", "-")
    for skill in ("alpha", "beta"):
        path = root / "skills" / skill / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {skill}\ndescription: {skill} fixture\n---\n\n# {skill}\n",
            encoding="utf-8",
        )
    script = root / "scripts" / "fixture_mcp.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env python3\nprint('fixture')\n", encoding="utf-8")
    _write_json(
        root / ".mcp.json",
        {
            "mcpServers": {
                "first": {"command": "python3", "args": ["./scripts/fixture_mcp.py"]},
                "second": {"command": "python3", "args": ["./scripts/fixture_mcp.py"]},
            }
        },
    )
    _write_json(
        root / "hooks.json",
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "./scripts/on-start.sh"}]}
                ]
            }
        },
    )
    _write_json(root / ".app.json", {"apps": {"fixture": {"id": "app_fixture"}}})
    manifest: dict[str, Any] = {
        "name": name,
        "id": plugin_id,
        "version": "1.2.3",
        "skills": "./skills/",
        "mcpServers": "./.mcp.json",
        "hooks": "./hooks.json",
        "apps": "./.app.json",
    }
    if extension:
        manifest["futureExecutor"] = {"path": "./scripts/unknown.py"}
    _write_json(root / ".codex-plugin" / "plugin.json", manifest)
    return root


def _observed(root: Path, *, marketplace: str = "fixture-marketplace"):
    return snapshot_installed_codex_plugin(
        root,
        source=CodexPluginSourceCoordinate(
            type="local",
            requested=str(root),
            resolved=f"codex-marketplace://{marketplace}/{root.name}@1.2.3",
            marketplace_name=marketplace,
        ),
    )


def _workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True)
    return workspace


def test_snapshot_store_copies_rehashes_and_detects_byte_and_mode_drift(
    tmp_path: Path,
) -> None:
    source = _plugin_root(tmp_path / "host")
    workspace = _workspace(tmp_path)
    store = CodexPluginSnapshotStore(workspace)
    before = _observed(source)

    committed = store.commit(before)
    stored_script = store.content_root(committed) / "scripts" / "fixture_mcp.py"
    source_script = source / "scripts" / "fixture_mcp.py"
    source_script.write_text("changed after admission\n", encoding="utf-8")

    assert store.load(committed.snapshot_digest) == committed
    assert stored_script.read_text(encoding="utf-8").startswith("#!/usr/bin/env")

    stored_script.chmod(stored_script.stat().st_mode ^ 0o100)
    with pytest.raises(StudioError, match="漂移") as raised:
        store.load(committed.snapshot_digest)
    assert raised.value.code == "CODEX_PLUGIN_SNAPSHOT_DRIFT"

    source_script.write_text("#!/usr/bin/env python3\nprint('fixture')\n", encoding="utf-8")
    without_exec = _observed(source)
    source_script.chmod(source_script.stat().st_mode | 0o100)
    with_exec = _observed(source)
    assert with_exec.artifact_digest != without_exec.artifact_digest


def test_lookup_is_read_only_until_snapshot_is_explicitly_committed(tmp_path: Path) -> None:
    source = _plugin_root(tmp_path / "host")
    workspace = _workspace(tmp_path)
    store = CodexPluginSnapshotStore(workspace)
    observed = _observed(source)

    assert not store.root.exists()
    assert store.lookup(observed) is None
    assert not store.root.exists()

    committed = store.commit(observed)
    assert store.lookup(observed) == committed


def test_installed_root_rejects_host_coordinate_traversal(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    with pytest.raises(StudioError) as raised:
        find_installed_codex_plugin_root(
            codex_home,
            marketplace_name="../outside",
            plugin_name="fixture-plugin",
            version="1.2.3",
        )
    assert raised.value.code == "CODEX_PLUGIN_CACHE_COORDINATE_INVALID"


def test_installed_root_rejects_symlink_escape_from_codex_cache(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    version_parent = (
        codex_home / "plugins" / "cache" / "fixture-marketplace" / "fixture-plugin"
    )
    version_parent.mkdir(parents=True)
    outside = _plugin_root(tmp_path / "outside")
    try:
        (version_parent / "1.2.3").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(StudioError) as raised:
        find_installed_codex_plugin_root(
            codex_home,
            marketplace_name="fixture-marketplace",
            plugin_name="fixture-plugin",
            version="1.2.3",
        )
    assert raised.value.code == "CODEX_PLUGIN_INSTALLED_ROOT_UNSAFE"


def test_marketplace_contains_only_selected_components_and_sorted_native_names(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = CodexPluginSnapshotStore(workspace)
    zeta = store.commit(
        _observed(
            _plugin_root(tmp_path / "zeta", name="zeta-plugin", plugin_id="aaa.plugin")
        )
    )
    alpha = store.commit(
        _observed(
            _plugin_root(tmp_path / "alpha", name="alpha-plugin", plugin_id="zzz.plugin")
        )
    )
    zeta_selected = zeta.select_components(("skill:alpha", "mcp:first"))
    alpha_selected = alpha.select_components(("skill:beta",))

    receipt = store.materialize_marketplace(
        plugin_lock_digest="sha256:" + "a" * 64,
        # Deliberately opposite native-name order.
        selections=((zeta, zeta_selected), (alpha, alpha_selected)),
    )

    assert receipt.plugin_names == ("alpha-plugin", "zeta-plugin")
    marketplace_root = store.verify_marketplace(receipt)
    payload = json.loads(
        (marketplace_root / ".agents" / "plugins" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["name"] for item in payload["plugins"]] == [
        "alpha-plugin",
        "zeta-plugin",
    ]
    materialized = snapshot_installed_codex_plugin(
        marketplace_root / "plugins" / "zeta-plugin",
        source=zeta.source,
    )
    assert {(item.kind, item.name) for item in materialized.components} == {
        ("skill", "alpha"),
        ("mcp", "first"),
    }
    assert not (marketplace_root / "plugins" / "zeta-plugin" / "skills" / "beta").exists()


def test_marketplace_rejects_duplicate_native_plugin_names(tmp_path: Path) -> None:
    store = CodexPluginSnapshotStore(_workspace(tmp_path))
    first = store.commit(
        _observed(_plugin_root(tmp_path / "one", name="same-name", plugin_id="one.plugin"))
    )
    second = store.commit(
        _observed(_plugin_root(tmp_path / "two", name="same-name", plugin_id="two.plugin"))
    )

    with pytest.raises(StudioError) as raised:
        store.materialize_marketplace(
            plugin_lock_digest="sha256:" + "c" * 64,
            selections=(
                (first, first.select_components(("skill:alpha",))),
                (second, second.select_components(("skill:beta",))),
            ),
        )
    assert raised.value.code == "CODEX_PLUGIN_MARKETPLACE_NAME_CONFLICT"


@pytest.mark.parametrize(
    "source",
    [
        CodexPluginSourceCoordinate(
            type="local",
            requested="/host/plugins/fixture-plugin",
            resolved="codex-marketplace://fixture-marketplace/fixture-plugin@1.2.3",
            marketplace_name="fixture-marketplace",
        ),
        CodexPluginSourceCoordinate(
            type="git",
            requested="https://example.test/fixture-plugin.git",
            resolved="https://example.test/fixture-plugin.git@" + "a" * 40,
            marketplace_name="fixture-marketplace",
        ),
        CodexPluginSourceCoordinate(
            type="npm",
            requested="@fixture/plugin@1.2.3",
            resolved="@fixture/plugin@1.2.3",
            marketplace_name="fixture-marketplace",
            registry="https://registry.npmjs.org",
            integrity="sha512-fixture-integrity",
        ),
    ],
    ids=("local", "git-full-sha", "npm-exact-with-integrity"),
)
def test_local_git_and_npm_coordinates_survive_snapshot_and_materialization(
    tmp_path: Path,
    source: CodexPluginSourceCoordinate,
) -> None:
    root = _plugin_root(tmp_path / source.type)
    observed = snapshot_installed_codex_plugin(root, source=source)
    store = CodexPluginSnapshotStore(_workspace(tmp_path))

    committed = store.commit(observed)
    receipt = store.materialize_marketplace(
        plugin_lock_digest="sha256:" + "d" * 64,
        selections=((committed, committed.select_components(("skill:alpha",))),),
    )

    assert committed.source == source
    assert (store.verify_marketplace(receipt) / "plugins" / "fixture-plugin").is_dir()


def test_selective_marketplace_fails_closed_on_unknown_manifest_extension(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = CodexPluginSnapshotStore(workspace)
    snapshot = store.commit(_observed(_plugin_root(tmp_path / "host", extension=True)))

    with pytest.raises(StudioError) as raised:
        store.materialize_marketplace(
            plugin_lock_digest="sha256:" + "b" * 64,
            selections=((snapshot, snapshot.select_components(("skill:alpha",))),),
        )
    assert raised.value.code == "CODEX_PLUGIN_MANIFEST_EXTENSION_UNSUPPORTED"
    assert raised.value.details == {"fields": ["futureExecutor"]}


def test_builder_compiles_native_binding_into_content_pinned_plugin_lock(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = CodexPluginSnapshotStore(workspace)
    snapshot = store.commit(_observed(_plugin_root(tmp_path / "host")))
    binding = NativePluginBinding(
        ecosystem="codex",
        plugin_ref=snapshot.plugin_ref,
        snapshot_digest=snapshot.snapshot_digest,
        components=["skill:alpha", "mcp:first"],
    )
    builder = CodexStudioBuilder(workspace, plugin_snapshot_store=store)

    lock, selections, statuses = builder._native_plugin_lock([binding])

    assert len(lock.plugins) == 1
    entry = lock.plugins[0]
    assert f"plugin://{entry.id}@{entry.version}" == snapshot.plugin_ref
    assert entry.digest == snapshot.artifact_digest
    assert entry.upstream == snapshot.source.to_plugin_source_snapshot()
    assert {item.id for item in entry.components} == {"skill:alpha", "mcp:first"}
    assert selections[0][0] == snapshot
    assert statuses[snapshot.plugin_ref]["runnable"] is True
    digest = plugin_lock_digest(lock)
    assert CodexStudioBuilder._build_id("f" * 64, {}) == "build_" + "f" * 20
    assert CodexStudioBuilder._build_id(
        "f" * 64, {}, plugin_lock_digest_value=digest
    ) != "build_" + "f" * 20


def test_hook_binding_is_fail_closed_before_runtime_bootstrap(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = CodexPluginSnapshotStore(workspace)
    snapshot = store.commit(_observed(_plugin_root(tmp_path / "host")))
    binding = NativePluginBinding(
        ecosystem="codex",
        plugin_ref=snapshot.plugin_ref,
        snapshot_digest=snapshot.snapshot_digest,
        components=["hook:SessionStart"],
    )
    lock, selections, statuses = CodexStudioBuilder(
        workspace, plugin_snapshot_store=store
    )._native_plugin_lock([binding])
    digest = plugin_lock_digest(lock)
    marketplace = store.materialize_marketplace(
        plugin_lock_digest=digest,
        selections=selections,
    )
    manifest = CodexAgentManifest(
        name="fixture-agent",
        version="1.0.0",
        runtime={"version": "0.1.0"},
        model="fixture-model",
        prompt="fixture",
        plugins=[binding],
    )
    build = SimpleNamespace(
        id="build_0123456789abcdef0123",
        plugin_lock=lock,
        plugin_lock_digest=digest,
        plugin_marketplace=marketplace,
        plugin_runtime_status=statuses,
    )

    with pytest.raises(StudioError) as raised:
        CodexRunSpecResolver(
            workspace, plugin_snapshot_store=store
        )._plugin_bootstrap(build, manifest)
    assert raised.value.code == "CODEX_PLUGIN_HOOK_TRUST_UNAVAILABLE"
    assert statuses[snapshot.plugin_ref]["hookTrust"] == "unsupported"


def _inventory(
    *,
    source: dict[str, Any],
    installed: bool = True,
    version: str | None = "1.2.3",
) -> CodexPluginInventory:
    return CodexPluginInventory.model_validate(
        {
            "pluginId": "fixture-plugin@fixture-marketplace",
            "name": "fixture-plugin",
            "marketplaceName": "fixture-marketplace",
            "marketplacePath": "/fixture/marketplace.json",
            "version": version,
            "installed": installed,
            "enabled": installed,
            "availability": "AVAILABLE",
            "source": source,
        }
    )


@pytest.mark.parametrize(
    "inventory",
    [
        _inventory(source={"type": "git", "url": "https://example.test/p.git", "refName": "main"}),
        _inventory(
            source={"type": "git", "url": "https://example.test/p.git", "sha": "deadbeef"}
        ),
        _inventory(source={"type": "npm", "package": "fixture", "version": "latest"}),
        _inventory(source={"type": "npm", "package": "fixture", "version": "^1.2.0"}),
        _inventory(source={"type": "local", "path": "/fixture"}, version=None),
    ],
)
def test_source_coordinate_rejects_mutable_git_npm_and_unknown_versions(
    inventory: CodexPluginInventory,
) -> None:
    with pytest.raises(StudioError) as raised:
        _codex_source_coordinate(inventory)
    assert raised.value.code == "CODEX_PLUGIN_SOURCE_NOT_IMMUTABLE"


def test_source_coordinate_retains_resolved_git_sha_and_npm_integrity() -> None:
    sha = "d" * 40
    git = _codex_source_coordinate(
        _inventory(
            source={
                "type": "git",
                "url": "https://example.test/p.git",
                "refName": "main",
                "sha": sha,
            }
        )
    )
    npm = _codex_source_coordinate(
        _inventory(
            source={
                "type": "npm",
                "package": "@fixture/plugin",
                "version": "1.2.3",
                "integrity": "sha512-fixture",
            }
        )
    )

    assert git.resolved.endswith("@" + sha)
    assert npm.requested == npm.resolved == "@fixture/plugin@1.2.3"
    assert npm.integrity == "sha512-fixture"


class _FakeCodexBridge:
    before_installed = True
    uninstall_calls: list[str] = []

    def __init__(self, **_options: Any) -> None:
        self.host = CodexBridgeHost(version="1.2.3")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def read_plugin(self, *_args: object, **_kwargs: object) -> CodexPluginDetail:
        inventory = _inventory(
            source={"type": "local", "path": "/fixture"},
            installed=self.before_installed,
        )
        return CodexPluginDetail(inventory=inventory)

    async def install_plugin(self, *_args: object, **_kwargs: object) -> CodexPluginInstallResult:
        return CodexPluginInstallResult(
            inventory=_inventory(source={"type": "local", "path": "/fixture"}),
            auth_policy="ON_INSTALL",
        )

    async def uninstall_plugin(self, plugin_id: str) -> CodexPluginUninstallResult:
        self.uninstall_calls.append(plugin_id)
        return CodexPluginUninstallResult(plugin_id=plugin_id)


def _plugin_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    workspace = _workspace(tmp_path)
    studio = SimpleNamespace(
        workspace=workspace,
        codex_plugin_snapshots=CodexPluginSnapshotStore(workspace),
    )
    app = FastAPI()

    @app.exception_handler(StudioError)
    async def handle_studio_error(_request: Request, error: StudioError):
        return JSONResponse(status_code=error.status_code, content=error.as_dict())

    monkeypatch.setattr(api_plugin_routes, "CodexAppServerPluginBridge", _FakeCodexBridge)
    register_plugin_routes(app, studio)
    return TestClient(app), studio


def test_plugin_get_does_not_create_a_workspace_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    installed = (
        codex_home
        / "plugins"
        / "cache"
        / "fixture-marketplace"
        / "fixture-plugin"
        / "1.2.3"
    )
    plugin = _plugin_root(tmp_path / "installed")
    installed.parent.mkdir(parents=True)
    os.rename(plugin, installed)
    monkeypatch.setenv("KSADK_CODEX_HOME", str(codex_home))
    _FakeCodexBridge.before_installed = True
    client, studio = _plugin_api(tmp_path, monkeypatch)

    response = client.get(
        "/api/v1/plugin-ecosystems/codex/plugins/fixture-plugin",
        params={"marketplace_name": "fixture-marketplace"},
    )

    assert response.status_code == 200
    assert response.json()["snapshot"] is None
    assert response.json()["snapshotRequired"] is True
    assert not studio.codex_plugin_snapshots.root.exists()


def test_explicit_snapshot_post_admits_existing_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    installed = (
        codex_home
        / "plugins"
        / "cache"
        / "fixture-marketplace"
        / "fixture-plugin"
        / "1.2.3"
    )
    plugin = _plugin_root(tmp_path / "installed")
    installed.parent.mkdir(parents=True)
    os.rename(plugin, installed)
    monkeypatch.setenv("KSADK_CODEX_HOME", str(codex_home))
    _FakeCodexBridge.before_installed = True
    client, studio = _plugin_api(tmp_path, monkeypatch)

    response = client.post(
        "/api/v1/plugin-ecosystems/codex/plugins/fixture-plugin:snapshot",
        json={"marketplaceName": "fixture-marketplace"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshotRequired"] is False
    assert payload["snapshot"]["snapshotDigest"].startswith("sha256:")
    assert studio.codex_plugin_snapshots.root.is_dir()


@pytest.mark.parametrize(
    ("preexisting", "expected_code", "expected_uninstalls"),
    [
        (False, "CODEX_PLUGIN_SNAPSHOT_FAILED_ROLLED_BACK", 1),
        (True, "CODEX_PLUGIN_INSTALLED_BUT_UNADMITTED", 0),
    ],
)
def test_install_snapshot_failure_has_explicit_compensation_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preexisting: bool,
    expected_code: str,
    expected_uninstalls: int,
) -> None:
    client, _studio = _plugin_api(tmp_path, monkeypatch)
    _FakeCodexBridge.before_installed = preexisting
    _FakeCodexBridge.uninstall_calls = []

    def fail_snapshot(*_args: object, **_kwargs: object):
        raise StudioError("TEST_SNAPSHOT_FAILURE", "fixture", status_code=409)

    monkeypatch.setattr(api_plugin_routes, "_commit_codex_snapshot", fail_snapshot)
    response = client.post(
        "/api/v1/plugin-ecosystems/codex/plugins/fixture-plugin:install",
        json={"marketplaceName": "fixture-marketplace", "acceptUndeclaredPermissions": True},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code
    assert len(_FakeCodexBridge.uninstall_calls) == expected_uninstalls
