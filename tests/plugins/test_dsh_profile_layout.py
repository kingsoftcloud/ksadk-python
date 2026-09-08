"""New managed profiles select a persistent layout without migrating existing ones."""

import json
from types import SimpleNamespace

import pytest
import yaml

from ksadk.plugins.bridges import dsh
from ksadk.plugins.bridges.dsh import DshPluginMutationError, DshProfilePluginBridge


def test_failed_first_install_removes_initialized_layout(tmp_path, monkeypatch):
    home = tmp_path / "dsh"
    profile = home / "profiles/test"
    source = tmp_path / "bundle.tgz"
    source.write_bytes(b"fixture")
    calls = []

    def native(args, cwd, environment):
        if "--version" in args:
            return SimpleNamespace(stdout="0.1.1-rc.2", stderr="")
        calls.append(args)
        assert "--config.node-linker=isolated" in args
        profile.mkdir(parents=True)
        (profile / "package.json").write_text(json.dumps({"dependencies": {"fixture": "1.0.0"}}))
        (profile / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (profile / "pnpm-workspace.yaml").write_text(
            "nodeLinker: hoisted\nautoInstallPeers: false\npackages: ['.']\n"
        )
        return SimpleNamespace(stdout="", stderr="")

    with DshProfilePluginBridge(
        dsh_home=home, profile="test", dsh_command=("fixture-dsh",), command_runner=native,
    ) as bridge:
        monkeypatch.setattr(bridge, "_prepare_source", lambda _: SimpleNamespace(
            command_source=str(source), digest="", kind="tgz", artifact="",
        ))

        def reject(_):
            settings = yaml.safe_load((profile / "pnpm-workspace.yaml").read_text())
            assert settings == {
                "nodeLinker": "isolated", "autoInstallPeers": False, "packages": ["."],
            }
            raise DshPluginMutationError("fixture preflight rejected")

        monkeypatch.setattr(bridge, "_require_bundle", reject)
        with pytest.raises(DshPluginMutationError, match="fixture preflight rejected"):
            bridge.install_plugin(str(source), accept_host_permissions=True)
    assert len(calls) == 1
    assert not profile.exists()


def test_existing_profile_layout_is_not_silently_changed(tmp_path):
    home = tmp_path / "dsh"
    profile = home / "profiles/test"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text("{}")
    settings = profile / "pnpm-workspace.yaml"
    original = "# user policy\nnodeLinker: hoisted\ncustomValue: keep\n"
    settings.write_text(original)
    calls = []

    def native(args, cwd, environment):
        calls.append(args)
        return SimpleNamespace(stdout="0.1.1-rc.2" if "--version" in args else "", stderr="")

    with DshProfilePluginBridge(
        dsh_home=home, profile="test", dsh_command=("fixture-dsh",), command_runner=native,
    ) as bridge:
        bridge._plugin_command("add", "fixture@1.0.0")
    assert not any("--config.node-linker=isolated" in call for call in calls)
    assert settings.read_text() == original


@pytest.mark.parametrize("failure", [None, "install", "lock", "preflight", "restore"])
def test_migration_restores_bytes_without_reinstall_on_failure(tmp_path, monkeypatch, failure):
    home = tmp_path / "dsh"
    profile = home / "profiles/test"
    modules = profile / "node_modules"
    modules.mkdir(parents=True)
    (modules / "original.js").write_text("original bytes")
    original_manifest = '{"dependencies": {}, "dsh": {"profile": {"bundles": []}}}'
    (profile / "package.json").write_text(original_manifest)
    (profile / "pnpm-lock.yaml").write_text("original lock")
    original_settings = "# preserve on rollback\nnodeLinker: hoisted\nautoInstallPeers: false\n"
    (profile / "pnpm-workspace.yaml").write_text(original_settings)
    installs = []

    def native(args, cwd, environment):
        if "--version" in args:
            return SimpleNamespace(stdout="0.1.1-rc.2", stderr="")
        if "install" in args:
            installs.append(args)
            assert not modules.exists()
            modules.mkdir()
            (modules / "new.js").write_text("new layout")
            if failure in {"install", "restore"}:
                raise DshPluginMutationError("install failed")
            if failure == "lock":
                (profile / "pnpm-lock.yaml").write_text("unexpected resolution")
        elif failure == "preflight":
            raise DshPluginMutationError("preflight failed")
        return SimpleNamespace(stdout="plugins: []", stderr="")

    with DshProfilePluginBridge(
        dsh_home=home, profile="test", dsh_command=("fixture-dsh",), command_runner=native,
    ) as bridge:
        if failure == "restore":
            def cannot_restore(*_args):
                raise OSError("fixture restoration failure")

            monkeypatch.setattr(dsh.os, "replace", cannot_restore)
        if failure:
            with pytest.raises(DshPluginMutationError):
                bridge.migrate_to_isolated_layout(accept_host_permissions=True)
            if failure == "restore":
                backups = list(profile.glob(".layout-backup-*"))
                assert len(backups) == 1
                assert (backups[0] / "node_modules/original.js").read_text() == "original bytes"
                assert (backups[0] / "profile-files/package.json").read_text() == original_manifest
                assert len(installs) == 1
                return
            assert (modules / "original.js").read_text() == "original bytes"
            assert not (modules / "new.js").exists()
            assert (profile / "pnpm-workspace.yaml").read_text() == original_settings
        else:
            bridge.migrate_to_isolated_layout(accept_host_permissions=True)
            assert (modules / "new.js").exists()
            assert yaml.safe_load((profile / "pnpm-workspace.yaml").read_text()) == {
                "nodeLinker": "isolated", "autoInstallPeers": False,
            }
        assert (profile / "package.json").read_text() == original_manifest
        assert (profile / "pnpm-lock.yaml").read_text() == "original lock"
        assert len(installs) == 1  # rollback restores bytes, never re-runs pnpm
        assert not list(profile.glob(".layout-backup-*"))


@pytest.mark.parametrize("node_linker", ["hoisted", "isolated"])
def test_owned_profile_can_recover_external_dependency_links(tmp_path, node_linker):
    home = tmp_path / "dsh"
    profile = home / "profiles/web"
    modules = profile / "node_modules"
    modules.mkdir(parents=True)
    external = tmp_path / "ambient-package"
    external.mkdir()
    (external / "index.js").write_text("ambient bytes")
    (modules / "ambient-package").symlink_to(external, target_is_directory=True)
    fallback = profile / ".dsh-module-fallback"
    fallback.mkdir()
    (fallback / "stale.txt").write_text("stale runtime cache")
    (profile / "package.json").write_text(
        '{"dependencies": {}, "dsh": {"profile": {"bundles": []}}}'
    )
    (profile / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (profile / "pnpm-workspace.yaml").write_text(
        f"nodeLinker: {node_linker}\nautoInstallPeers: false\n"
    )
    installs = []

    def native(args, cwd, environment):
        if "--version" in args:
            return SimpleNamespace(stdout="0.1.1-rc.2", stderr="")
        if "install" in args:
            installs.append(args)
            assert not modules.exists()
            modules.mkdir()
            (modules / "isolated.js").write_text("isolated bytes")
        return SimpleNamespace(stdout="plugins: []", stderr="")

    with DshProfilePluginBridge(
        dsh_home=home, profile="web", dsh_command=("fixture-dsh",), command_runner=native,
    ) as bridge:
        bridge.migrate_to_isolated_layout(
            accept_host_permissions=True,
            recover_external_dependency_links=True,
        )

    assert len(installs) == 1
    assert (modules / "isolated.js").read_text() == "isolated bytes"
    assert not (modules / "ambient-package").exists()
    assert not fallback.exists()
    assert yaml.safe_load((profile / "pnpm-workspace.yaml").read_text())["nodeLinker"] == (
        "isolated"
    )
    assert not list(profile.glob(".layout-backup-*"))
