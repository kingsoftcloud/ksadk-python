from __future__ import annotations

import hashlib
import json
import multiprocessing
import time
from pathlib import Path

import pytest

from ksadk.plugins.bridges import dsh as dsh_bridge
from ksadk.plugins.bridges.dsh import (
    DshHostUnavailableError,
    DshPluginApprovalRequired,
    DshPluginMutationError,
    DshProfilePluginBridge,
)

PLUGIN_NAME = "@example/dsh-plugin"
NEW_PLUGIN_NAME = "@example/new-dsh-plugin"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _profile(home: Path, *, enabled: bool = True) -> Path:
    root = home / "profiles" / "test-profile"
    _write_json(
        root / "package.json",
        {
            "dependencies": {PLUGIN_NAME: "1.2.3"},
            "dsh": {
                "profile": {
                    "bundles": ["@deepseek-ai/dsh-base", *([PLUGIN_NAME] if enabled else [])]
                }
            },
        },
    )
    package = root / "node_modules" / "@example" / "dsh-plugin"
    _write_json(
        package / "package.json",
        {
            "name": PLUGIN_NAME,
            "displayName": "Example plugin",
            "description": "fixture",
            "version": "1.2.3",
            "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
        },
    )
    (package / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    return root


def _pristine_web_profile(home: Path) -> Path:
    root = home / "profiles" / "web"
    _write_json(
        root / "package.json",
        {
            "name": "dsh-profile-web",
            "private": True,
            "dependencies": {},
            "dsh": {
                "profile": {
                    "bundles": [
                        "@deepseek-ai/dsh-base",
                        "@deepseek-ai/dsh-web-app",
                    ],
                    "patchReload": "live",
                }
            },
        },
    )
    (root / "pnpm-workspace.yaml").write_text("packages: []\n", encoding="utf-8")
    return root


class Runner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.fail_preflight = False

    def __call__(self, command, _cwd, environment):
        self.commands.append(tuple(command))
        assert environment["DSH_HOME"]
        if command[-1] == "--version":
            return dsh_bridge._CommandResult(stdout="0.1.1-rc.2\n")
        if command[-1] == "--dump-config":
            if self.fail_preflight:
                raise DshPluginMutationError("invalid composition")
            return dsh_bridge._CommandResult(stdout="- id: fixture\n")
        return dsh_bridge._CommandResult()


def _blocking_mutation_worker(
    home_value: str,
    ready_value: str,
    release_value: str,
    outcome_value: str,
) -> None:
    home = Path(home_value)
    ready = Path(ready_value)
    release = Path(release_value)
    outcome = Path(outcome_value)

    class BlockingRunner(Runner):
        def __call__(self, command, cwd, environment):
            if command[-1] == "--dump-config":
                ready.write_text("locked", encoding="utf-8")
                deadline = time.monotonic() + 10
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not release.exists():
                    raise RuntimeError("test did not release mutation")
            return super().__call__(command, cwd, environment)

    try:
        bridge = DshProfilePluginBridge(
            dsh_home=home,
            profile="test-profile",
            dsh_command=("dsh-fixture",),
            command_runner=BlockingRunner(),
        )
        bridge.start()
        bridge.set_enabled(PLUGIN_NAME, enabled=False)
        outcome.write_text("ok", encoding="utf-8")
    except BaseException as error:
        outcome.write_text(f"error:{error!r}", encoding="utf-8")


def _read_profile_worker(
    home_value: str,
    operation: str,
    started_value: str,
    outcome_value: str,
) -> None:
    try:
        bridge = DshProfilePluginBridge(
            dsh_home=Path(home_value),
            profile="test-profile",
            dsh_command=("dsh-fixture",),
            command_runner=Runner(),
        )
        bridge.start()
        Path(started_value).write_text("started", encoding="utf-8")
        if operation == "list":
            value = str(bridge.list_plugins()[0].enabled)
        else:
            value = "project" if bridge.project_profile().bundles else "empty"
        Path(outcome_value).write_text(f"ok:{value}", encoding="utf-8")
    except BaseException as error:
        Path(outcome_value).write_text(f"error:{error!r}", encoding="utf-8")


def test_bridge_projects_inventory_and_digest_without_raw_profile(tmp_path: Path) -> None:
    home = tmp_path / "dsh-home"
    _profile(home)
    runner = Runner()
    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="test-profile",
        dsh_command=("dsh-fixture",),
        command_runner=runner,
    )

    assert bridge.start().version == "0.1.1-rc.2"
    assert bridge.list_plugins()[0].model_dump(mode="json", by_alias=True) == {
        "ecosystem": "dsh",
        "integrationMode": "bridged",
        "profile": "test-profile",
        "name": PLUGIN_NAME,
        "displayName": "Example plugin",
        "description": "fixture",
        "version": "1.2.3",
        "requestedSpec": "1.2.3",
        "sourceDigest": None,
        "sourceKind": None,
        "clientExtension": False,
        "settingsIntegration": False,
        "installed": True,
        "enabled": True,
        "permissionsDeclared": False,
        "riskDisclosures": [
            "DSH packages and install scripts run with the native host user privileges.",
            "DSH bundle manifests do not declare a complete runtime permission set.",
        ],
    }
    projection = bridge.project_profile()
    assert projection.bundles == ("@deepseek-ai/dsh-base", PLUGIN_NAME)
    assert projection.config_digest.startswith("sha256:")
    assert projection.config_bytes > 0
    assert not hasattr(projection, "config")


def test_bridge_reports_client_and_settings_contributions(tmp_path: Path) -> None:
    home = tmp_path / "dsh-home"
    root = _profile(home)
    package_path = root / "node_modules" / "@example" / "dsh-plugin" / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["dsh"]["client"] = {
        "platform": "web",
        "inject": ["@deepseek-ai/dsh-client-ui-settings"],
    }
    _write_json(package_path, package)
    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="test-profile",
        dsh_command=("dsh-fixture",),
        command_runner=Runner(),
    )

    bridge.start()
    item = bridge.list_plugins()[0]

    assert item.client_extension is True
    assert item.settings_integration is True


def test_local_source_receipt_fails_closed_when_immutable_archive_changes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "dsh-home"
    root = _profile(home)
    content = b"immutable npm archive"
    digest_hex = hashlib.sha256(content).hexdigest()
    artifact = home / "immutable-plugin-sources" / digest_hex / "package.tgz"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    dependency_spec = f"file:../../immutable-plugin-sources/{digest_hex}/package.tgz"
    manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    manifest["dependencies"][PLUGIN_NAME] = dependency_spec
    _write_json(root / "package.json", manifest)
    _write_json(
        root / ".ksadk-dsh-plugins.json",
        {
            "version": 2,
            "order": [PLUGIN_NAME],
            "disabled": [],
            "sources": {
                PLUGIN_NAME: {
                    "digest": f"sha256:{digest_hex}",
                    "kind": "tgz",
                    "artifact": f"immutable-plugin-sources/{digest_hex}/package.tgz",
                    "dependencySpec": dependency_spec,
                }
            },
        },
    )
    runner = Runner()
    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="test-profile",
        dsh_command=("dsh-fixture",),
        command_runner=runner,
    )
    bridge.start()

    item = bridge.get_plugin(PLUGIN_NAME)
    assert item.source_digest == f"sha256:{digest_hex}"
    assert bridge.project_profile().config_digest.startswith("sha256:")

    before = (root / "package.json").read_bytes()
    artifact.write_bytes(b"mutated")
    with pytest.raises(DshPluginMutationError, match="digest changed"):
        bridge.project_profile()
    with pytest.raises(DshPluginMutationError, match="digest changed"):
        bridge.set_enabled(PLUGIN_NAME, enabled=False)
    assert (root / "package.json").read_bytes() == before


@pytest.mark.parametrize("source", ["file:/tmp/plugin", "link:/tmp/plugin"])
def test_mutable_package_manager_source_schemes_are_rejected(
    tmp_path: Path, source: str
) -> None:
    bridge = DshProfilePluginBridge(
        dsh_home=tmp_path,
        dsh_command=("dsh-fixture",),
        command_runner=Runner(),
    )
    bridge.start()

    with pytest.raises(ValueError, match="exact"):
        bridge.install_plugin(source, accept_host_permissions=True)


def test_enable_disable_is_transactional_and_preflighted(tmp_path: Path) -> None:
    home = tmp_path / "dsh-home"
    root = _profile(home)
    runner = Runner()
    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="test-profile",
        dsh_command=("dsh-fixture",),
        command_runner=runner,
    )
    bridge.start()

    assert bridge.set_enabled(PLUGIN_NAME, enabled=False).enabled is False
    manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert PLUGIN_NAME not in manifest["dsh"]["profile"]["bundles"]
    assert bridge.set_enabled(PLUGIN_NAME, enabled=True).enabled is True

    before = (root / "package.json").read_bytes()
    runner.fail_preflight = True
    with pytest.raises(DshPluginMutationError, match="invalid composition"):
        bridge.set_enabled(PLUGIN_NAME, enabled=False)
    assert (root / "package.json").read_bytes() == before


@pytest.mark.parametrize("source", [NEW_PLUGIN_NAME, f"{NEW_PLUGIN_NAME}@2.0.0"])
def test_install_is_disabled_until_explicit_enable_without_deactivating_existing_bundle(
    tmp_path: Path, source: str,
) -> None:
    home = tmp_path / "dsh-home"
    root = _profile(home, enabled=True)
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    class InstallRunner(Runner):
        def __call__(self, command, cwd, environment):  # noqa: ANN001
            if command[:2] == ("npm", "view"):
                assert command == ("npm", "view", f"{NEW_PLUGIN_NAME}@latest", "version", "--json")
                return dsh_bridge._CommandResult(stdout='"2.0.0"')
            if len(command) >= 2 and command[-2] == "add":
                assert command[-1] == f"{NEW_PLUGIN_NAME}@2.0.0"
                manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
                manifest["dependencies"][NEW_PLUGIN_NAME] = "2.0.0"
                manifest["dsh"]["profile"]["bundles"].append(NEW_PLUGIN_NAME)
                _write_json(root / "package.json", manifest)
                package = root / "node_modules" / "@example" / "new-dsh-plugin"
                _write_json(
                    package / "package.json",
                    {
                        "name": NEW_PLUGIN_NAME,
                        "displayName": "New plugin",
                        "version": "2.0.0",
                        "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
                    },
                )
                (package / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
                return dsh_bridge._CommandResult()
            return super().__call__(command, cwd, environment)

    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="test-profile",
        dsh_command=("dsh-fixture",),
        command_runner=InstallRunner(),
    )
    bridge.start()

    installed = bridge.install_plugin(
        source,
        accept_host_permissions=True,
    )

    assert installed.installed is True
    assert installed.enabled is False
    state = json.loads((root / ".ksadk-dsh-plugins.json").read_text(encoding="utf-8"))
    assert state["disabled"] == [NEW_PLUGIN_NAME]
    assert bridge.get_plugin(PLUGIN_NAME).enabled is True
    assert bridge.project_profile().bundles == ("@deepseek-ai/dsh-base", PLUGIN_NAME)

    enabled = bridge.set_enabled(NEW_PLUGIN_NAME, enabled=True)
    assert enabled.enabled is True
    assert bridge.project_profile().bundles == (
        "@deepseek-ai/dsh-base",
        PLUGIN_NAME,
        NEW_PLUGIN_NAME,
    )


@pytest.mark.parametrize("was_enabled", [False, True])
def test_failed_upgrade_restores_the_previous_activation_state(
    tmp_path: Path,
    was_enabled: bool,
) -> None:
    home = tmp_path / "dsh-home"
    root = _profile(home, enabled=was_enabled)
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    class UpgradeRunner(Runner):
        def __call__(self, command, cwd, environment):  # noqa: ANN001
            if len(command) >= 2 and command[-2] == "update":
                manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
                manifest["dependencies"][PLUGIN_NAME] = "2.0.0"
                _write_json(root / "package.json", manifest)
                package_path = root / "node_modules" / "@example" / "dsh-plugin" / "package.json"
                package = json.loads(package_path.read_text(encoding="utf-8"))
                package["version"] = "2.0.0"
                _write_json(package_path, package)
                self.fail_preflight = True
                return dsh_bridge._CommandResult()
            if len(command) >= 2 and command[-2] == "install":
                package_path = root / "node_modules" / "@example" / "dsh-plugin" / "package.json"
                package = json.loads(package_path.read_text(encoding="utf-8"))
                package["version"] = "1.2.3"
                _write_json(package_path, package)
                return dsh_bridge._CommandResult()
            return super().__call__(command, cwd, environment)

    runner = UpgradeRunner()
    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="test-profile",
        dsh_command=("dsh-fixture",),
        command_runner=runner,
    )
    bridge.start()
    bridge.set_enabled(PLUGIN_NAME, enabled=was_enabled)
    before_manifest = (root / "package.json").read_bytes()
    before_lock = (root / "pnpm-lock.yaml").read_bytes()
    before_state = (root / ".ksadk-dsh-plugins.json").read_bytes()

    with pytest.raises(DshPluginMutationError, match="invalid composition"):
        bridge.update_plugin(PLUGIN_NAME, accept_host_permissions=True)

    assert (root / "package.json").read_bytes() == before_manifest
    assert (root / "pnpm-lock.yaml").read_bytes() == before_lock
    assert (root / ".ksadk-dsh-plugins.json").read_bytes() == before_state
    restored = bridge.get_plugin(PLUGIN_NAME)
    assert restored.version == "1.2.3"
    assert restored.enabled is was_enabled


@pytest.mark.parametrize(
    ("operation", "expected"),
    [("list", "ok:False"), ("project", "ok:project")],
)
def test_profile_reads_wait_for_cross_process_mutation_transaction(
    tmp_path: Path, operation: str, expected: str
) -> None:
    home = tmp_path / "dsh-home"
    _profile(home)
    ready = tmp_path / "mutation-ready"
    release = tmp_path / "mutation-release"
    mutation_outcome = tmp_path / "mutation-outcome"
    reader_started = tmp_path / "reader-started"
    reader_outcome = tmp_path / "reader-outcome"
    context = multiprocessing.get_context("spawn")
    mutation = context.Process(
        target=_blocking_mutation_worker,
        args=(str(home), str(ready), str(release), str(mutation_outcome)),
    )
    reader = context.Process(
        target=_read_profile_worker,
        args=(str(home), operation, str(reader_started), str(reader_outcome)),
    )
    mutation.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "mutation did not reach its preflight window"

        reader.start()
        deadline = time.monotonic() + 5
        while not reader_started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reader_started.exists(), "reader did not begin"
        time.sleep(0.2)
        assert not reader_outcome.exists(), "list escaped the profile transaction lock"

        release.write_text("continue", encoding="utf-8")
        mutation.join(timeout=5)
        reader.join(timeout=5)
        assert mutation.exitcode == reader.exitcode == 0
        assert mutation_outcome.read_text(encoding="utf-8") == "ok"
        assert reader_outcome.read_text(encoding="utf-8") == expected
    finally:
        release.touch(exist_ok=True)
        for process in (mutation, reader):
            if process.pid is not None and process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=2)


def test_dsh_commands_receive_only_explained_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "dsh-home"
    _profile(home)
    allowed = {
        "PATH": "/test/bin",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "HTTPS_PROXY": "http://proxy.invalid:8080",
        "NO_PROXY": "127.0.0.1",
        "SSL_CERT_FILE": str(tmp_path / "ca.pem"),
        "NODE_EXTRA_CA_CERTS": str(tmp_path / "node-ca.pem"),
        "LANG": "C.UTF-8",
    }
    for name, value in allowed.items():
        monkeypatch.setenv(name, value)
    sensitive = {
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "NPM_TOKEN",
        "GITHUB_TOKEN",
        "KSADK_TEST_PRIVATE_CREDENTIAL",
        "NODE_OPTIONS",
        "SSH_AUTH_SOCK",
    }
    for name in sensitive:
        monkeypatch.setenv(name, f"secret-{name.lower()}")

    environments: list[dict[str, str]] = []

    def runner(command, _cwd, environment):  # noqa: ANN001, ANN202
        environments.append(dict(environment))
        if command[-1] == "--version":
            return dsh_bridge._CommandResult(stdout="0.1.1-rc.2\n")
        return dsh_bridge._CommandResult(stdout="- id: fixture\n")

    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="test-profile",
        dsh_command=("dsh-fixture",),
        command_runner=runner,
    )
    bridge.start()
    bridge.project_profile()

    assert environments
    for environment in environments:
        assert environment["DSH_HOME"] == str(home.resolve())
        assert {name: environment[name] for name in allowed} == allowed
        assert not sensitive.intersection(environment)


def test_install_requires_explicit_host_permission_acceptance(tmp_path: Path) -> None:
    bridge = DshProfilePluginBridge(
        dsh_home=tmp_path,
        dsh_command=("dsh-fixture",),
        command_runner=Runner(),
    )
    bridge.start()
    with pytest.raises(DshPluginApprovalRequired):
        bridge.install_plugin("@deepseek-ai/dsh-subagent-codex")


@pytest.mark.parametrize(
    "response", ['null', '["1.0.0", "2.0.0"]', '"latest"', '"file:/tmp/pkg"', 'not json']
)
def test_latest_resolution_failure_does_not_mutate_profile(tmp_path: Path, response: str) -> None:
    home = tmp_path / "dsh-home"
    root = _profile(home)
    before = (root / "package.json").read_bytes()
    commands = []

    def runner(command, cwd, environment):
        commands.append(command)
        return dsh_bridge._CommandResult(stdout=response)

    bridge = DshProfilePluginBridge(
        dsh_home=home, profile="test-profile", dsh_command=("dsh-fixture",), command_runner=runner,
    )
    with pytest.raises(DshPluginMutationError, match="exact version"):
        bridge.install_plugin(NEW_PLUGIN_NAME, accept_host_permissions=True)
    assert commands == [("npm", "view", f"{NEW_PLUGIN_NAME}@latest", "version", "--json")]
    assert (root / "package.json").read_bytes() == before


def test_failed_first_install_removes_new_profile_tree(tmp_path: Path) -> None:
    home = tmp_path / "dsh-home"

    class FailedInstallRunner(Runner):
        def __call__(self, command, cwd, environment):
            if command[-1] == "--version":
                return super().__call__(command, cwd, environment)
            profile_root = home / "profiles" / "web"
            _write_json(profile_root / "package.json", {"dependencies": {}})
            (profile_root / "node_modules").mkdir(parents=True)
            raise DshPluginMutationError("install failed")

    bridge = DshProfilePluginBridge(
        dsh_home=home,
        dsh_command=("dsh-fixture",),
        command_runner=FailedInstallRunner(),
    )
    bridge.start()

    with pytest.raises(DshPluginMutationError, match="install failed"):
        bridge.install_plugin("@deepseek-ai/dsh-subagent-codex@1.0.0", accept_host_permissions=True)

    assert not (home / "profiles" / "web").exists()


def test_install_refuses_unmanaged_existing_profile_directory(tmp_path: Path) -> None:
    home = tmp_path / "dsh-home"
    unmanaged = home / "profiles" / "web"
    unmanaged.mkdir(parents=True)
    marker = unmanaged / "keep.txt"
    marker.write_text("user-owned", encoding="utf-8")
    bridge = DshProfilePluginBridge(
        dsh_home=home,
        dsh_command=("dsh-fixture",),
        command_runner=Runner(),
    )
    bridge.start()

    with pytest.raises(DshPluginMutationError, match="no package manifest"):
        bridge.install_plugin("@deepseek-ai/dsh-subagent-codex@1.0.0", accept_host_permissions=True)

    assert marker.read_text(encoding="utf-8") == "user-owned"


def test_failed_first_install_restores_pristine_official_web_profile(tmp_path: Path) -> None:
    home = tmp_path / "dsh-home"
    root = _pristine_web_profile(home)
    before_manifest = (root / "package.json").read_bytes()
    before_workspace = (root / "pnpm-workspace.yaml").read_bytes()

    class FailedWebInstallRunner(Runner):
        def __call__(self, command, cwd, environment):  # noqa: ANN001
            if len(command) >= 2 and command[-2] == "add":
                manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
                manifest["dependencies"][NEW_PLUGIN_NAME] = "2.0.0"
                _write_json(root / "package.json", manifest)
                (root / "pnpm-lock.yaml").write_text(
                    "lockfileVersion: '9.0'\n", encoding="utf-8"
                )
                (root / "node_modules" / "partial").mkdir(parents=True)
                raise DshPluginMutationError("simulated install failure")
            return super().__call__(command, cwd, environment)

    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="web",
        dsh_command=("dsh-fixture",),
        command_runner=FailedWebInstallRunner(),
    )
    bridge.start()

    with pytest.raises(DshPluginMutationError, match="simulated install failure"):
        bridge.install_plugin(
            f"{NEW_PLUGIN_NAME}@2.0.0",
            accept_host_permissions=True,
        )

    assert (root / "package.json").read_bytes() == before_manifest
    assert (root / "pnpm-workspace.yaml").read_bytes() == before_workspace
    assert not (root / "pnpm-lock.yaml").exists()
    assert not (root / "node_modules").exists()


def test_package_update_refuses_profile_without_rollback_lock(tmp_path: Path) -> None:
    home = tmp_path / "dsh-home"
    _profile(home)
    bridge = DshProfilePluginBridge(
        dsh_home=home,
        profile="test-profile",
        dsh_command=("dsh-fixture",),
        command_runner=Runner(),
    )
    bridge.start()

    with pytest.raises(DshPluginMutationError, match="no pnpm lockfile"):
        bridge.update_plugin(PLUGIN_NAME, accept_host_permissions=True)


def test_missing_host_is_typed_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dsh_bridge.shutil, "which", lambda _name: None)
    bridge = DshProfilePluginBridge(dsh_home=tmp_path)
    with pytest.raises(DshHostUnavailableError, match="not installed"):
        bridge.start()


@pytest.mark.parametrize("profile", ["../escape", "with/slash", "", "x" * 65])
def test_profile_name_cannot_escape_dsh_home(tmp_path: Path, profile: str) -> None:
    with pytest.raises(ValueError, match="simple name"):
        DshProfilePluginBridge(dsh_home=tmp_path, profile=profile)
