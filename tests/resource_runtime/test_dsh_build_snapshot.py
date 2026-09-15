import json
import shutil
from types import SimpleNamespace

import pytest

from ksadk.plugins.bridges.dsh import DshPluginMutationError, DshProfilePluginBridge
from ksadk.plugins.dsh_installation_digest import DshInstallationDigestError, installation_digest


@pytest.fixture
def installed(tmp_path):
    home = tmp_path / "dsh"
    profile = home / "profiles" / "studio"
    modules = profile / "node_modules"
    package = modules / ".pnpm" / "tool@1" / "node_modules" / "tool"
    package.mkdir(parents=True)
    (package / "index.mjs").write_text("export const value = 1")
    (modules / "tool").symlink_to(package.relative_to(modules))
    (profile / "package.json").write_text(json.dumps({"dependencies": {"tool": "1.0.0"}}))
    (profile / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    state = {"config": "plugins: []\n"}

    def run(args, cwd, environment):
        return SimpleNamespace(
            stdout="0.1.1-rc.2" if "--version" in args else state["config"], stderr=""
        )

    bridge = DshProfilePluginBridge(
        dsh_home=home, profile="studio", dsh_command=["fixture-dsh"], command_runner=run
    )
    bridge.start()
    return bridge, profile, package, state


def test_snapshot_verifies_current_installation_without_storing_raw_config(installed):
    bridge, _, _, state = installed
    state["config"] = "credential: fixture-private-value\n"
    snapshot = bridge.snapshot_for_build()
    bridge.verify_build_snapshot(snapshot)
    assert "fixture-private-value" not in snapshot.model_dump_json()


@pytest.mark.parametrize("change", ["code", "lock", "config", "added", "execute-bit"])
def test_build_verification_rejects_actual_execution_input_changes(installed, change):
    bridge, profile, package, state = installed
    snapshot = bridge.snapshot_for_build()
    if change == "code":
        (package / "index.mjs").write_text("export const value = 2")
    elif change == "lock":
        (profile / "pnpm-lock.yaml").write_text("changed")
    elif change == "config":
        state["config"] = "changed\n"
    elif change == "added":
        (package / "extra.mjs").write_text("extra")
    else:
        (package / "index.mjs").chmod(0o700)
    with pytest.raises(DshPluginMutationError, match="no longer matches"):
        bridge.verify_build_snapshot(snapshot)


def test_tree_digest_is_location_independent_and_tracks_internal_links(tmp_path, installed):
    _, profile, _, _ = installed
    original = profile / "node_modules"
    copied = tmp_path / "copied"
    shutil.copytree(original, copied, symlinks=True)
    assert installation_digest(original) == installation_digest(copied)
    (copied / "tool").unlink()
    assert installation_digest(original) != installation_digest(copied)


def test_external_dependency_is_not_a_closed_build_input(tmp_path, installed):
    bridge, profile, _, _ = installed
    outside = tmp_path / "outside"
    outside.mkdir()
    (profile / "node_modules" / "external").symlink_to(outside)
    with pytest.raises(DshInstallationDigestError, match="escapes"):
        bridge.snapshot_for_build()


@pytest.mark.parametrize("limits", [{"max_files": 1}, {"max_bytes": 1}])
def test_installed_tree_limits(installed, limits):
    _, profile, _, _ = installed
    with pytest.raises(DshInstallationDigestError, match="limit"):
        installation_digest(profile / "node_modules", **limits)
