"""Tests for the source-checkout-free DSH plugin development workflow."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ksadk.plugins.bridges import dsh as dsh_bridge
from ksadk.plugins.bridges.dsh import DshPluginInventory, DshProfileProjection
from ksadk.plugins.dsh_toolchain import (
    DSH_CORE_PACKAGES,
    DSH_GITHUB_COMMIT,
    DSH_GITHUB_TAG,
    DSH_PACKAGE,
    DSH_VERSION,
    PNPM_VERSION,
    CommandResult,
    DshPluginDeveloper,
    DshPluginSourceError,
    DshToolchainManager,
    DshToolchainUnavailableError,
    DshToolchainVersionMismatchError,
)


class _InstallRunner:
    def __init__(self, *, dsh_version: str = DSH_VERSION) -> None:
        self.dsh_version = dsh_version
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, command, cwd: Path, _environment) -> CommandResult:
        argv = tuple(command)
        self.calls.append((argv, cwd))
        if argv[-1] == "--version":
            if Path(argv[0]).name == "pnpm":
                return CommandResult(stdout=f"{PNPM_VERSION}\n")
            return CommandResult(stdout=f"dsh {self.dsh_version}\n")
        if argv[1:3] == ("install", "--lockfile-only"):
            (cwd / "pnpm-lock.yaml").write_text(
                "lockfileVersion: '9.0'\n"
                "importers:\n"
                "  .:\n"
                "    dependencies:\n"
                f"      '{DSH_PACKAGE}':\n"
                f"        specifier: {DSH_VERSION}\n"
                f"        version: {DSH_VERSION}\n",
                encoding="utf-8",
            )
            return CommandResult()
        if argv[1:3] == ("install", "--frozen-lockfile"):
            target = cwd / "node_modules" / DSH_PACKAGE / "lib" / "bin.js"
            target.parent.mkdir(parents=True)
            target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (target.parents[1] / "package.json").write_text(
                json.dumps(
                    {
                        "name": DSH_PACKAGE,
                        "version": DSH_VERSION,
                        "dependencies": {
                            package: f"^{DSH_VERSION}" for package in DSH_CORE_PACKAGES
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            for package in DSH_CORE_PACKAGES:
                manifest = cwd / "node_modules" / Path(*package.split("/")) / "package.json"
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(
                    json.dumps({"name": package, "version": DSH_VERSION}) + "\n",
                    encoding="utf-8",
                )
            dependency = cwd / "node_modules" / ".pnpm" / "cordis" / "index.js"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("export class Context {}\n", encoding="utf-8")
            executable = cwd / "node_modules" / ".bin" / "dsh"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(Path("..") / DSH_PACKAGE / "lib" / "bin.js")
            return CommandResult()
        if len(argv) >= 3 and argv[1] == "-e":
            return CommandResult(
                stdout=str(cwd / "node_modules" / ".pnpm" / "cordis" / "index.js")
            )
        raise AssertionError(argv)


def _fake_executable(tmp_path: Path, name: str = "pnpm") -> Path:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_managed_toolchain_installs_published_dsh_without_source_checkout(tmp_path: Path) -> None:
    runner = _InstallRunner()
    pnpm = _fake_executable(tmp_path)
    manager = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(pnpm),),
        command_runner=runner,
    )

    state = manager.install()

    assert state.usable is True
    assert state.actual_version == DSH_VERSION
    manifest = json.loads((manager.root / "package.json").read_text(encoding="utf-8"))
    assert manifest == {
        "name": "agentengine-managed-dsh-toolchain",
        "private": True,
        "packageManager": f"pnpm@{PNPM_VERSION}",
        "dependencies": {DSH_PACKAGE: DSH_VERSION},
    }
    assert (manager.root / "pnpm-lock.yaml").is_file()
    assert Path(state.executable or "").resolve().is_relative_to(manager.root)
    receipt = json.loads((manager.root / "toolchain.json").read_text(encoding="utf-8"))
    assert receipt["githubTag"] == DSH_GITHUB_TAG
    assert receipt["githubCommit"] == DSH_GITHUB_COMMIT
    assert receipt["distribution"] == "core"
    assert receipt["corePackages"] == list(DSH_CORE_PACKAGES)
    install_argv = [call[0][1:] for call in runner.calls if len(call[0]) > 1]
    assert (
        "install",
        "--lockfile-only",
        "--ignore-scripts",
        "--config.auto-install-peers=true",
    ) in install_argv
    assert (
        "install",
        "--frozen-lockfile",
        "--ignore-scripts",
        "--config.auto-install-peers=true",
    ) in install_argv
    assert not any("deepseek-harness" in str(path) for _argv, path in runner.calls)


def test_managed_toolchain_reports_actual_dsh_version_mismatch(tmp_path: Path) -> None:
    runner = _InstallRunner(dsh_version="0.1.0")
    pnpm = _fake_executable(tmp_path)
    manager = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(pnpm),),
        command_runner=runner,
    )
    manager.root.mkdir(parents=True)
    manager._write_install_manifest(manager.root)
    (manager.root / "pnpm-lock.yaml").write_text(
        f"'{DSH_PACKAGE}': {DSH_VERSION}\n",
        encoding="utf-8",
    )
    target = manager.root / "node_modules" / DSH_PACKAGE / "lib" / "bin.js"
    target.parent.mkdir(parents=True)
    target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    executable = manager.root / "node_modules" / ".bin" / "dsh"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(Path("..") / DSH_PACKAGE / "lib" / "bin.js")

    state = manager.status()

    assert state.usable is False
    assert state.problem == "version_mismatch"
    assert state.actual_version == "0.1.0"
    with pytest.raises(DshToolchainVersionMismatchError):
        manager.require_command()


def test_toolchain_install_fails_typed_when_pnpm_is_missing(tmp_path: Path) -> None:
    manager = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(tmp_path / "missing-pnpm"),),
    )

    with pytest.raises(DshToolchainUnavailableError):
        manager.install()

    assert not manager.root.exists()


def test_missing_managed_dsh_has_an_actionable_typed_failure(tmp_path: Path) -> None:
    pnpm = _fake_executable(tmp_path)
    manager = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(pnpm),),
        command_runner=_InstallRunner(),
    )

    state = manager.status()

    assert state.installed is False
    assert state.problem == "not_installed"
    with pytest.raises(DshToolchainUnavailableError, match="toolchain install"):
        manager.require_command()


def test_install_replaces_a_bounded_half_install_atomically(tmp_path: Path) -> None:
    runner = _InstallRunner()
    pnpm = _fake_executable(tmp_path)
    manager = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(pnpm),),
        command_runner=runner,
    )
    manager.root.mkdir(parents=True)
    (manager.root / "package.json").write_text("{}\n", encoding="utf-8")
    (manager.root / "half-installed.txt").write_text("partial", encoding="utf-8")

    state = manager.install()

    assert state.usable is True
    assert not (manager.root / "half-installed.txt").exists()
    assert not list(manager.root.parent.glob(f".{DSH_VERSION}.backup-*"))
    assert not list(manager.root.parent.glob(f".{DSH_VERSION}.install-*"))


def test_concurrent_installs_serialize_and_publish_one_complete_root(tmp_path: Path) -> None:
    class SlowInstallRunner(_InstallRunner):
        def __call__(self, command, cwd: Path, environment) -> CommandResult:
            if tuple(command)[1:3] == ("install", "--lockfile-only"):
                time.sleep(0.1)
            return super().__call__(command, cwd, environment)

    runner = SlowInstallRunner()
    pnpm = _fake_executable(tmp_path)

    def install_once():
        return DshToolchainManager(
            base_dir=tmp_path / "config",
            pnpm_command=(str(pnpm),),
            command_runner=runner,
        ).install()

    with ThreadPoolExecutor(max_workers=2) as executor:
        states = tuple(executor.map(lambda _value: install_once(), range(2)))

    assert all(state.usable for state in states)
    lock_only_calls = [
        argv
        for argv, _cwd in runner.calls
        if argv[1:3] == ("install", "--lockfile-only")
    ]
    assert len(lock_only_calls) == 1
    root = tmp_path / "config" / "dsh" / DSH_VERSION
    assert (root / "pnpm-lock.yaml").is_file()
    assert not list(root.parent.glob(f".{DSH_VERSION}.install-*"))


def test_toolchain_rejects_executable_that_escapes_managed_root(tmp_path: Path) -> None:
    runner = _InstallRunner()
    pnpm = _fake_executable(tmp_path)
    manager = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(pnpm),),
        command_runner=runner,
    )
    manager.root.mkdir(parents=True)
    manager._write_install_manifest(manager.root)
    (manager.root / "pnpm-lock.yaml").write_text(
        f"'{DSH_PACKAGE}': {DSH_VERSION}\n",
        encoding="utf-8",
    )
    outside = _fake_executable(tmp_path, "outside-dsh")
    executable = manager.root / "node_modules" / ".bin" / "dsh"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(outside)

    state = manager.status()

    assert state.usable is False
    assert state.problem == "executable_outside_managed_root"


def test_toolchain_resolves_dependency_from_pinned_installation(tmp_path: Path) -> None:
    runner = _InstallRunner()
    pnpm = _fake_executable(tmp_path)
    manager = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(pnpm),),
        command_runner=runner,
    )
    manager.install()

    resolved = manager.resolve_module_entry("@deepseek-ai/cordis")

    assert resolved.is_relative_to(manager.root)
    assert resolved.read_text(encoding="utf-8") == "export class Context {}\n"


def test_toolchain_rejects_invalid_or_escaping_dependency(tmp_path: Path) -> None:
    runner = _InstallRunner()
    pnpm = _fake_executable(tmp_path)
    manager = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(pnpm),),
        command_runner=runner,
    )
    manager.install()

    with pytest.raises(DshToolchainUnavailableError, match="invalid"):
        manager.resolve_module_entry("../../ambient-package")

    outside = tmp_path / "ambient.js"
    outside.write_text("export {}\n", encoding="utf-8")

    def escaping_runner(command, cwd: Path, environment) -> CommandResult:
        if tuple(command)[1:2] == ("-e",):
            return CommandResult(stdout=str(outside))
        return runner(command, cwd, environment)

    escaped = DshToolchainManager(
        base_dir=tmp_path / "config",
        pnpm_command=(str(pnpm),),
        command_runner=escaping_runner,
    )
    with pytest.raises(DshToolchainUnavailableError, match="escapes"):
        escaped.resolve_module_entry("@deepseek-ai/cordis")


def test_create_generates_only_a_standard_dsh_bundle(tmp_path: Path) -> None:
    target = tmp_path / "weather-panel"

    result = DshPluginDeveloper().create(target)

    package = json.loads((target / "package.json").read_text(encoding="utf-8"))
    assert result.package_name == "dsh-weather-panel"
    assert package["dsh"] == {"bundle": {"patch": "./cordis.patch.yml"}}
    assert package["main"] == "index.js"
    assert package["types"] == "index.d.ts"
    assert (target / "index.js").is_file()
    assert (target / "index.d.ts").is_file()
    assert "dsh-weather-panel" in (target / "cordis.patch.yml").read_text(encoding="utf-8")
    assert not (target / "ksadk-plugin.yaml").exists()
    assert not any("ksadk" in key.casefold() for key in package)


def test_create_refuses_to_overwrite_an_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()

    with pytest.raises(DshPluginSourceError, match="already exists"):
        DshPluginDeveloper().create(target)


class _StaticToolchain:
    def __init__(self, executable: Path) -> None:
        self.executable = executable

    def require_command(self, _explicit=None):
        return (str(self.executable),)

    def require_pnpm(self):
        return (str(self.executable),)


class _LifecycleBridge:
    calls: list[str] = []

    def __init__(self, **_kwargs) -> None:
        self.host = type("Host", (), {"version": DSH_VERSION})()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def install_plugin(self, _source: str, *, accept_host_permissions: bool):
        assert accept_host_permissions is True
        self.calls.append("install")
        return DshPluginInventory(
            profile="validate",
            name="dsh-weather-panel",
            display_name="Weather panel",
            version="0.1.0",
            requested_spec="link:fixture",
            enabled=False,
        )

    def project_profile(self):
        self.calls.append("project")
        return DshProfileProjection(
            profile="validate",
            bundles=("@deepseek-ai/dsh-base", "dsh-weather-panel"),
            config_digest="sha256:" + "a" * 64,
            config_bytes=100,
            host_version=DSH_VERSION,
        )

    def set_enabled(self, _name: str, *, enabled: bool):
        self.calls.append("enable" if enabled else "disable")

    def uninstall_plugin(self, _name: str) -> None:
        self.calls.append("uninstall")


def test_validate_executes_the_full_isolated_dsh_lifecycle(tmp_path: Path) -> None:
    target = tmp_path / "weather"
    DshPluginDeveloper().create(target, package_name="dsh-weather-panel")
    executable = _fake_executable(tmp_path, "dsh")
    _LifecycleBridge.calls = []
    developer = DshPluginDeveloper(
        toolchain=_StaticToolchain(executable),  # type: ignore[arg-type]
        bridge_factory=_LifecycleBridge,  # type: ignore[arg-type]
    )

    result = developer.validate(target)

    assert result.host_version == DSH_VERSION
    assert _LifecycleBridge.calls == [
        "install",
        "project",
        "disable",
        "enable",
        "uninstall",
    ]


def test_validate_accepts_a_standard_npm_tarball_source(tmp_path: Path) -> None:
    archive = tmp_path / "dsh-weather-panel-0.1.0.tgz"
    archive.write_bytes(b"tgz")
    executable = _fake_executable(tmp_path, "dsh")
    _LifecycleBridge.calls = []

    result = DshPluginDeveloper(
        toolchain=_StaticToolchain(executable),  # type: ignore[arg-type]
        bridge_factory=_LifecycleBridge,  # type: ignore[arg-type]
    ).validate(archive)

    assert result.package_name == "dsh-weather-panel"
    assert _LifecycleBridge.calls[-1] == "uninstall"


class _RejectingSourceBridge(_LifecycleBridge):
    def install_plugin(self, _source: str, *, accept_host_permissions: bool):
        assert accept_host_permissions is True
        raise ValueError(
            "DSH registry source must be <package>@<exact-semver>; "
            "Git URLs, tags, and ranges are not allowed"
        )


def test_validate_classifies_unpinned_registry_coordinate_as_source_error(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path, "dsh")
    developer = DshPluginDeveloper(
        toolchain=_StaticToolchain(executable),  # type: ignore[arg-type]
        bridge_factory=_RejectingSourceBridge,  # type: ignore[arg-type]
    )

    with pytest.raises(DshPluginSourceError, match="exact-semver"):
        developer.validate("@xmanrui/dsh-im")


def test_pack_delegates_to_exact_pnpm_argv_and_produces_standard_tgz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "weather"
    DshPluginDeveloper().create(target, package_name="dsh-weather-panel")
    executable = _fake_executable(tmp_path, "pnpm")
    calls: list[tuple[str, ...]] = []
    environments: list[dict[str, str]] = []
    monkeypatch.setenv("PATH", "/test/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NPM_TOKEN", "must-not-reach-pack-script")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-pack-script")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/tmp/untrusted.js")

    def runner(command, _cwd: Path, environment) -> CommandResult:
        argv = tuple(command)
        calls.append(argv)
        environments.append(dict(environment))
        destination = Path(argv[argv.index("--pack-destination") + 1])
        (destination / "dsh-weather-panel-0.1.0.tgz").write_bytes(b"tgz")
        return CommandResult(stdout="dsh-weather-panel-0.1.0.tgz\n")

    result = DshPluginDeveloper(
        toolchain=_StaticToolchain(executable),  # type: ignore[arg-type]
        command_runner=runner,
    ).pack(target)

    assert Path(result.artifact).is_file()
    assert calls == [
        (
            str(executable),
            "pack",
            "--pack-destination",
            str((target / "dist").resolve()),
        )
    ]
    assert len(environments) == 1
    environment = environments[0]
    assert environment["PATH"] == "/test/bin"
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["COREPACK_ENABLE_PROJECT_SPEC"] == "0"
    assert not {"NPM_TOKEN", "OPENAI_API_KEY", "NODE_OPTIONS"}.intersection(environment)
    assert set(environment) <= {
        *dsh_bridge._DSH_SUBPROCESS_ENV_KEYS,
        "COREPACK_ENABLE_PROJECT_SPEC",
    }
