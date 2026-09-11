"""Release graph fencing and non-destructive managed toolchain upgrades."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ksadk.plugins.dsh_toolchain import (
    DSH_CORE_PACKAGES,
    DSH_GITHUB_COMMIT,
    DSH_GITHUB_TAG,
    DSH_PACKAGE,
    DSH_VERSION,
    PNPM_VERSION,
    CommandResult,
    DshToolchainInstallError,
    DshToolchainManager,
    _dependency_policy,
)


class Installer:
    def __init__(self) -> None:
        self.installs = 0
        self.fail_install = False
        self.fail_final_version = False

    def __call__(self, command, cwd, environment):
        if command[-1] == "--version":
            if Path(command[0]).name == "pnpm":
                return CommandResult(stdout=PNPM_VERSION, stderr="")
            version = (
                "0.1.5-rc.2" if self.fail_final_version and cwd.name == DSH_VERSION else DSH_VERSION
            )
            return CommandResult(stdout=version, stderr="")
        self.installs += 1
        if self.fail_install:
            raise DshToolchainInstallError("injected install failure")
        if "--lockfile-only" in command:
            (cwd / "pnpm-lock.yaml").write_text(
                f"lockfileVersion: '9.0'\npackages:\n  '{DSH_PACKAGE}@{DSH_VERSION}': {{}}\n"
            )
        else:
            executable = cwd / "node_modules/.bin/dsh"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture executable")
            for name in (DSH_PACKAGE, *DSH_CORE_PACKAGES):
                package = cwd / "node_modules" / name / "package.json"
                package.parent.mkdir(parents=True, exist_ok=True)
                package.write_text(
                    json.dumps(
                        {
                            "name": name,
                            "version": DSH_VERSION,
                            "dependencies": {item: f"^{DSH_VERSION}" for item in DSH_CORE_PACKAGES},
                        }
                    )
                )
        return CommandResult(stdout="", stderr="")


def manager_at(tmp_path: Path, installer: Installer) -> DshToolchainManager:
    return DshToolchainManager(base_dir=tmp_path, pnpm_command=("pnpm",), command_runner=installer)


def test_upgrade_isolated_from_old_release_and_receipt_fences_exact_graph(tmp_path: Path) -> None:
    previous = tmp_path / "dsh/0.1.2-rc.1"
    previous.mkdir(parents=True)
    (previous / "old-session.jsonl").write_text("old-format-data\n")
    installer = Installer()
    manager = manager_at(tmp_path, installer)
    assert manager.install().usable
    receipt = json.loads((manager.root / "toolchain.json").read_text())
    assert receipt["githubCommit"] == DSH_GITHUB_COMMIT
    assert receipt["githubTag"] == DSH_GITHUB_TAG
    assert receipt["schemaVersion"] == 2
    assert receipt["distribution"] == "core"
    assert receipt["lockDigest"].startswith("sha256:")
    assert receipt["dependencyPolicyDigest"].startswith("sha256:")
    assert (previous / "old-session.jsonl").read_text() == "old-format-data\n"
    assert manager.install().usable
    assert installer.installs == 2


def test_installed_core_version_is_checked_not_just_cli_version(tmp_path: Path) -> None:
    manager = manager_at(tmp_path, Installer())
    manager.install()
    package = manager.root / "node_modules" / DSH_CORE_PACKAGES[0] / "package.json"
    value = json.loads(package.read_text())
    value["version"] = "0.1.5-rc.2"
    package.write_text(json.dumps(value))
    assert manager.status().problem == "core_packages_missing_or_mismatched"


def test_changed_lock_or_resolution_policy_invalidates_installation(tmp_path: Path) -> None:
    manager = manager_at(tmp_path, Installer())
    manager.install()
    lock = manager.root / "pnpm-lock.yaml"
    lock.write_text(lock.read_text() + "# changed after validation\n")
    assert manager.status().problem == "receipt_missing_or_mismatched"
    manager.install()
    policy = manager.root / ".pnpmfile.cjs"
    policy.write_text(policy.read_text() + "// changed after validation\n")
    assert manager.status().problem == "manifest_mismatch"


def test_caret_resolution_cannot_accept_later_dsh_candidate(tmp_path: Path) -> None:
    manager = manager_at(tmp_path, Installer())
    manager.install()
    lock = manager.root / "pnpm-lock.yaml"
    lock.write_text(lock.read_text() + "  '@deepseek-ai/dsh-session@0.1.5-rc.2': {}\n")
    assert manager.status().problem == "lockfile_missing_or_mismatched"


@pytest.mark.parametrize("failure", ["fail_install", "fail_final_version"])
def test_failed_replacement_restores_preexisting_directory(tmp_path: Path, failure: str) -> None:
    installer = Installer()
    manager = manager_at(tmp_path, installer)
    manager.install()
    (manager.root / "keep.txt").write_text("existing install")
    (manager.root / "toolchain.json").unlink()
    setattr(installer, failure, True)
    with pytest.raises(DshToolchainInstallError):
        manager.install()
    assert (manager.root / "keep.txt").read_text() == "existing install"
    assert not list(manager.root.parent.glob(f".{DSH_VERSION}.install-*"))
    assert not list(manager.root.parent.glob(f".{DSH_VERSION}.backup-*"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for the pnpm policy")
def test_resolution_policy_pins_caret_but_rejects_conflicting_exact_source(tmp_path: Path) -> None:
    hook = tmp_path / ".pnpmfile.cjs"
    hook.write_text(_dependency_policy())
    script = (
        "const result = require(process.argv[1]).hooks.readPackage(JSON.parse(process.argv[2]));"
        "console.log(JSON.stringify(result))"
    )

    def resolve(spec: str):
        return subprocess.run(
            [
                "node",
                "-e",
                script,
                str(hook),
                json.dumps(
                    {"dependencies": {"@deepseek-ai/dsh-tools": spec, "other-package": "^2.0.0"}}
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    result = resolve("^0.1.5-rc.1")
    assert result.returncode == 0
    assert json.loads(result.stdout)["dependencies"] == {
        "@deepseek-ai/dsh-tools": DSH_VERSION,
        "other-package": "^2.0.0",
    }
    assert resolve("0.1.5-rc.2").returncode != 0
    assert resolve("file:/tmp/custom-dsh.tgz").returncode != 0
