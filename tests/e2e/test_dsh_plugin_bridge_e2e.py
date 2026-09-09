from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ksadk.plugins.bridges.dsh import DshPluginMutationError, DshProfilePluginBridge

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_DSH_PLUGIN_E2E") != "1",
    reason="set KSADK_DSH_PLUGIN_E2E=1 with a built DSH CLI entry to run",
)


def test_real_dsh_profile_plugin_lifecycle(tmp_path: Path) -> None:
    node = shutil.which("node")
    entry_value = os.environ.get("KSADK_DSH_PLUGIN_E2E_ENTRY", "").strip()
    assert node is not None, "Node.js is required for the real DSH bridge E2E"
    assert entry_value, "KSADK_DSH_PLUGIN_E2E_ENTRY must point to DSH lib/bin.js"
    entry = Path(entry_value).expanduser().resolve()
    assert entry.is_file(), f"DSH entry does not exist: {entry}"

    fixture = Path(__file__).parent / "fixtures" / "dsh-profile-plugin"
    mutable_source = tmp_path / "mutable-source"
    shutil.copytree(fixture, mutable_source)
    with DshProfilePluginBridge(
        dsh_home=tmp_path / "dsh-home",
        profile="ksadk-e2e",
        dsh_command=(node, str(entry)),
    ) as bridge:
        assert bridge.list_plugins() == ()
        installed = bridge.install_plugin(
            str(mutable_source),
            accept_host_permissions=True,
        )
        assert installed.name == "@ksadk-e2e/dsh-profile-plugin"
        assert installed.enabled is False
        assert installed.source_kind == "directory"
        assert installed.source_digest is not None

        state = json.loads(
            (tmp_path / "dsh-home" / "profiles" / "ksadk-e2e" / ".ksadk-dsh-plugins.json")
            .read_text(encoding="utf-8")
        )
        receipt = state["sources"][installed.name]
        assert receipt["digest"] == installed.source_digest
        immutable_archive = tmp_path / "dsh-home" / receipt["artifact"]
        assert immutable_archive.is_file()

        # Editing the developer checkout cannot alter the installed package:
        # it was packed with npm files semantics and installed from the digest store.
        (mutable_source / "index.mjs").write_text(
            "export const installedMarker = 'changed-after-install'\n", encoding="utf-8"
        )
        assert bridge.project_profile().config_digest.startswith("sha256:")

        projection = bridge.project_profile()
        assert installed.name not in projection.bundles
        assert projection.config_digest.startswith("sha256:")

        enabled = bridge.set_enabled(installed.name, enabled=True)
        assert enabled.enabled is True
        assert installed.name in bridge.project_profile().bundles

        # A broken explicit local upgrade must restore manifest, lock, state,
        # node_modules and the executable package selected before the attempt.
        broken = tmp_path / "broken-upgrade"
        shutil.copytree(fixture, broken)
        package_path = broken / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["version"] = "2.0.0"
        package_path.write_text(json.dumps(package), encoding="utf-8")
        (broken / "index.mjs").write_text(
            "export const installedMarker = 'broken-v2'\n", encoding="utf-8"
        )
        (broken / "cordis.patch.yml").write_text("- invalid: [\n", encoding="utf-8")
        before_manifest = (
            tmp_path / "dsh-home" / "profiles" / "ksadk-e2e" / "package.json"
        ).read_bytes()
        before_lock = (
            tmp_path / "dsh-home" / "profiles" / "ksadk-e2e" / "pnpm-lock.yaml"
        ).read_bytes()
        before_state = (
            tmp_path / "dsh-home" / "profiles" / "ksadk-e2e" / ".ksadk-dsh-plugins.json"
        ).read_bytes()
        with pytest.raises(DshPluginMutationError):
            bridge.update_plugin(
                installed.name,
                source=str(broken),
                accept_host_permissions=True,
            )
        profile_root = tmp_path / "dsh-home" / "profiles" / "ksadk-e2e"
        assert (profile_root / "package.json").read_bytes() == before_manifest
        assert (profile_root / "pnpm-lock.yaml").read_bytes() == before_lock
        assert (profile_root / ".ksadk-dsh-plugins.json").read_bytes() == before_state
        restored = bridge.get_plugin(installed.name)
        assert restored.version == "1.0.0"
        assert restored.source_digest == installed.source_digest
        installed_entry = (
            profile_root
            / "node_modules"
            / "@ksadk-e2e"
            / "dsh-profile-plugin"
            / "index.mjs"
        )
        executed = subprocess.run(
            [
                node,
                "--input-type=module",
                "--eval",
                (
                    "import {installedMarker} from "
                    + json.dumps(installed_entry.as_uri())
                    + "; console.log(installedMarker)"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert executed.stdout.strip() == "immutable-v1"
        assert installed.name in bridge.project_profile().bundles

        bridge.uninstall_plugin(installed.name)
        assert bridge.list_plugins() == ()


@pytest.mark.skipif(
    os.environ.get("KSADK_DSH_OFFICIAL_PLUGIN_E2E") != "1",
    reason="set KSADK_DSH_OFFICIAL_PLUGIN_E2E=1 to install the pinned official bundle",
)
def test_real_dsh_profile_installs_official_codex_bundle(tmp_path: Path) -> None:
    """Prove that the bridge delegates an official Bundle to the real DSH host."""

    node = shutil.which("node")
    entry_value = os.environ.get("KSADK_DSH_PLUGIN_E2E_ENTRY", "").strip()
    assert node is not None, "Node.js is required for the real DSH bridge E2E"
    assert entry_value, "KSADK_DSH_PLUGIN_E2E_ENTRY must point to DSH lib/bin.js"
    entry = Path(entry_value).expanduser().resolve()
    assert entry.is_file(), f"DSH entry does not exist: {entry}"

    source = os.environ.get(
        "KSADK_DSH_OFFICIAL_PLUGIN_SPEC",
        "@deepseek-ai/dsh-subagent-codex@0.1.2-rc.1",
    ).strip()
    assert source.startswith("@deepseek-ai/dsh-subagent-codex@")

    with DshProfilePluginBridge(
        dsh_home=tmp_path / "dsh-home",
        profile="ksadk-official-e2e",
        dsh_command=(node, str(entry)),
    ) as bridge:
        installed = bridge.install_plugin(source, accept_host_permissions=True)
        assert installed.name == "@deepseek-ai/dsh-subagent-codex"
        assert installed.version == source.rsplit("@", 1)[1]
        assert installed.enabled is False

        projection = bridge.project_profile()
        # The DSH host and the independently published Bundle intentionally
        # evolve on separate version lines.  The inventory must report both
        # truthfully instead of assuming the Bundle version pins the host.
        assert projection.host_version == bridge.host.version
        assert projection.host_version != installed.version
        assert installed.name not in projection.bundles
        assert projection.config_digest.startswith("sha256:")
        assert projection.config_bytes > 0

        enabled = bridge.set_enabled(installed.name, enabled=True)
        assert enabled.enabled is True
        assert bridge.project_profile().bundles[-1] == installed.name

        bridge.uninstall_plugin(installed.name)
        assert bridge.list_plugins() == ()
