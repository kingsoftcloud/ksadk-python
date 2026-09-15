"""Opt-in real npm packing and DSH CLI lifecycle for private resource bundles."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

from tests.test_runtime_common_packaging import REPO_ROOT

pytestmark = pytest.mark.skipif(
    not os.environ.get("KSADK_TEST_RESOURCE_CLI_ROOT"),
    reason="Requires isolated pinned DSH/pnpm installation at KSADK_TEST_RESOURCE_CLI_ROOT",
)


def test_private_resource_bundles_pack_install_enable_disable_uninstall(tmp_path):
    root = Path(os.environ["KSADK_TEST_RESOURCE_CLI_ROOT"]).resolve()
    pnpm = root / "pnpm/node_modules/.bin/pnpm"
    dsh = root / "toolchains/dsh/0.1.1-rc.2/node_modules/.bin/dsh"
    assert pnpm.is_file() and dsh.is_file()
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "PATH": str(pnpm.parent) + os.pathsep + os.environ["PATH"],
        "HOME": str(home),
        "KSADK_DSH_HOME": str(tmp_path / "dsh-home"),
        "KSADK_DSH_PROFILE": "resource-acceptance",
        "KSADK_DSH_BIN": str(dsh),
        "AGENTENGINE_PLUGIN_TOOLCHAIN_HOME": str(root / "toolchains"),
        "AGENTENGINE_PNPM_BIN": str(pnpm),
    }
    for name in ("LANG", "LC_ALL", "TMPDIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    evidence = {"commands": [], "packages": []}

    def cli(*arguments):
        result = subprocess.run(
            [sys.executable, "-I", "-m", "ksadk", "plugin", *arguments, "--output", "json"],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=180,
        )
        evidence["commands"].append({"arguments": arguments, "exitCode": result.returncode})
        (tmp_path / "cli-evidence.json").write_text(json.dumps(evidence, indent=2))
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    status = cli("toolchain", "status")
    assert status["usable"] and status["actualVersion"] == "0.1.1-rc.2"
    assert status["pnpmVersion"] == "11.7.0"
    packages = []
    for directory_name in (
        "dsh-platform-resources", "dsh-knowledge", "dsh-memory", "dsh-skill-center",
        "ksadk-dsh-capability-host",
    ):
        source = tmp_path / "sources" / directory_name
        shutil.copytree(REPO_ROOT / "ksadk/plugins/providers/bundles" / directory_name, source)
        manifest = json.loads((source / "package.json").read_bytes())
        packed = cli("pack", str(source), "--output-dir", str(tmp_path / "artifacts"))
        archive = Path(packed["artifact"])
        assert archive.is_relative_to(tmp_path / "artifacts")
        with tarfile.open(archive) as package:
            members = package.getmembers()
            assert all(member.isfile() and member.name.startswith("package/") for member in members)
            assert {member.name for member in members} == {
                "package/package.json", *("package/" + name for name in manifest["files"]),
            }
            for name in manifest["files"]:
                assert package.extractfile("package/" + name).read() == (source / name).read_bytes()
            shipped = json.load(package.extractfile("package/package.json"))
            assert shipped["name"] == manifest["name"]
            assert shipped["version"] == manifest["version"]
        packages.append((manifest["name"], archive))
        evidence["packages"].append({
            "name": manifest["name"], "version": manifest["version"],
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        })

    for name, archive in packages:
        installed = cli("install", str(archive), "--accept-host-permissions")
        assert installed["item"]["name"] == name
        assert installed["item"]["enabled"] is False
        enabled = cli("enable", name)
        assert enabled["item"]["enabled"] is True

    # Install an unchanged upstream UI bundle from npm, alongside our packages.
    # This proves profile/lifecycle compatibility only, not rendered browser UI.
    # pnpm 11 requires a separate native-build decision. Approve only the exact
    # dependency observed in this pinned upstream bundle, in this temporary home.
    profile = tmp_path / "dsh-home/profiles/resource-acceptance"
    settings_path = profile / "pnpm-workspace.yaml"
    settings = yaml.safe_load(settings_path.read_text()) if settings_path.exists() else {}
    assert settings["nodeLinker"] == "isolated"
    settings.setdefault("allowBuilds", {})["koffi@3.2.1"] = True
    settings_path.write_text(yaml.safe_dump(settings))
    external = "@deepseek-ai/dsh-web-app"
    upstream = cli("install", external + "@0.1.1-rc.2", "--accept-host-permissions")
    assert upstream["item"]["version"] == "0.1.1-rc.2"
    evidence["upstreamBundle"] = {"name": external, "version": "0.1.1-rc.2"}
    evidence["nativeBuildApproval"] = {"koffi@3.2.1": True}
    cli("enable", external)
    expected = {external, *(name for name, _ in packages)}
    inventory = cli("list")
    assert {item["name"] for item in inventory["items"] if item["enabled"]} == expected
    # The native CLI retains its built-in base bundle in every composed profile.
    assert set(cli("profile")["profile"]["bundles"]) == expected | {"@deepseek-ai/dsh-base"}
    for name, _ in reversed(packages):
        disabled = cli("disable", name)
        assert disabled["item"]["enabled"] is False
        cli("uninstall", name)
    remaining = cli("list")["items"]
    assert len(remaining) == 1
    assert remaining[0]["name"] == external and remaining[0]["enabled"]
    assert set(cli("profile")["profile"]["bundles"]) == {"@deepseek-ai/dsh-base", external}
    cli("disable", external)
    cli("uninstall", external)
    assert cli("list")["items"] == []
    assert cli("profile")["profile"]["bundles"] == ["@deepseek-ai/dsh-base"]
    (tmp_path / "cli-evidence.json").write_text(json.dumps(evidence, indent=2))
