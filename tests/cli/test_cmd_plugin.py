"""CLI coverage for the DSH-default and Codex-compatible plugin surface."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from ksadk.cli import _register_commands, cli
from ksadk.cli.cmd_plugin import (
    CODEX_HOME_ENV,
    DSH_BIN_ENV,
    DSH_HOME_ENV,
    DSH_PROFILE_ENV,
    plugin,
)
from ksadk.cli.error_utils import EXIT_CODE_VALIDATION
from ksadk.cli.global_options import ensure_global_cli_options
from ksadk.plugins.bridges.codex import (
    CodexBridgeHost,
    CodexPluginDetail,
    CodexPluginInstallResult,
    CodexPluginInventory,
    CodexPluginUninstallResult,
)
from ksadk.plugins.bridges.dsh import (
    DshBridgeHost,
    DshPluginInventory,
    DshProfileProjection,
)

ensure_global_cli_options(plugin)


def test_plugin_group_is_registered_on_root_cli() -> None:
    _register_commands()
    assert cli.commands["plugin"] is plugin


def test_plugin_help_exposes_only_dsh_default_and_codex_compatibility() -> None:
    result = CliRunner().invoke(plugin, ["--help"])

    assert result.exit_code == 0, result.output
    assert set(plugin.commands) == {
        "codex",
        "create",
        "disable",
        "dsh",
        "enable",
        "info",
        "install",
        "list",
        "pack",
        "profile",
        "test",
        "toolchain",
        "uninstall",
        "update",
        "validate",
    }
    for removed in ("init", "conformance"):
        assert removed not in plugin.commands
    lowered = result.output.casefold()
    for retired_public_concept in (
        "ksadk-plugin",
        "python entry",
        "python 入口",
        "ksadk 原生插件",
    ):
        assert retired_public_concept.casefold() not in lowered
    assert "dsh" in lowered
    assert "codex" in lowered


def test_missing_dsh_host_is_a_typed_failure(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        plugin,
        ["--output", "json", "list"],
        env={
            DSH_BIN_ENV: str(tmp_path / "missing-dsh"),
            DSH_HOME_ENV: str(tmp_path / "dsh-home"),
        },
    )

    assert result.exit_code == EXIT_CODE_VALIDATION
    assert json.loads(result.output)["error"] == {
        "code": "dsh_plugin_host_unavailable",
        "message": "DSH 插件宿主当前不可用",
        "details": {},
    }


def _codex_cli_inventory(*, installed: bool = False) -> CodexPluginInventory:
    return CodexPluginInventory(
        plugin_id="review@official",
        name="review",
        marketplace_name="official",
        marketplace_path="/private/marketplace/marketplace.json",
        version="1.0.0",
        installed=installed,
        enabled=installed,
        availability="AVAILABLE",
        source={"type": "local", "path": "/private/plugin"},
    )


class _FakeCodexCLIHost:
    homes: list[Path] = []
    installed = False
    acceptances: list[bool] = []

    def __init__(self, *, codex_home: Path) -> None:
        self.host = CodexBridgeHost(version="0.148.0")
        type(self).homes.append(codex_home)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None

    async def list_plugins(self, *, force_refetch: bool = False):
        del force_refetch
        return (_codex_cli_inventory(installed=type(self).installed),)

    async def read_plugin(self, plugin_id: str, *, marketplace_name: str | None = None):
        assert plugin_id == "review@official"
        assert marketplace_name in {None, "official"}
        return CodexPluginDetail(
            inventory=_codex_cli_inventory(installed=type(self).installed),
            description="Review plugin",
            skills=("review:code",),
            mcp_servers=("review-mcp",),
        )

    async def install_plugin(
        self,
        plugin_id: str,
        *,
        marketplace_name: str | None,
        accept_undeclared_permissions: bool,
    ):
        assert plugin_id == "review@official"
        assert marketplace_name == "official"
        type(self).acceptances.append(accept_undeclared_permissions)
        type(self).installed = True
        return CodexPluginInstallResult(
            inventory=_codex_cli_inventory(installed=True),
            auth_policy="ON_USE",
        )

    async def uninstall_plugin(self, plugin_id: str):
        assert plugin_id == "review@official"
        type(self).installed = False
        return CodexPluginUninstallResult(plugin_id=plugin_id)


def test_codex_cli_uses_bounded_app_server_bridge_and_explicit_permission_acceptance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakeCodexCLIHost.homes = []
    _FakeCodexCLIHost.installed = False
    _FakeCodexCLIHost.acceptances = []
    monkeypatch.setattr(
        "ksadk.plugins.bridges.codex.CodexAppServerPluginBridge",
        _FakeCodexCLIHost,
    )
    environment = {CODEX_HOME_ENV: str(tmp_path / "codex-home")}
    runner = CliRunner()

    catalog = runner.invoke(plugin, ["--output", "json", "codex", "list"], env=environment)
    assert catalog.exit_code == 0, catalog.output
    catalog_payload = json.loads(catalog.output)
    assert catalog_payload["host"]["version"] == "0.148.0"
    assert catalog_payload["items"][0]["integrationMode"] == "bridged"
    assert "/private" not in catalog.output

    detail = runner.invoke(
        plugin,
        [
            "--output",
            "json",
            "codex",
            "info",
            "review@official",
            "--marketplace",
            "official",
        ],
        env=environment,
    )
    assert detail.exit_code == 0, detail.output
    assert json.loads(detail.output)["capabilities"]["skills"] == ["review:code"]

    rejected = runner.invoke(
        plugin,
        ["--output", "json", "codex", "install", "review@official"],
        env=environment,
    )
    assert rejected.exit_code == EXIT_CODE_VALIDATION
    assert json.loads(rejected.output)["error"]["code"] == (
        "codex_plugin_permission_confirmation_required"
    )
    assert _FakeCodexCLIHost.acceptances == []

    installed = runner.invoke(
        plugin,
        [
            "--output",
            "json",
            "codex",
            "install",
            "review@official",
            "--marketplace",
            "official",
            "--accept-host-permissions",
        ],
        env=environment,
    )
    assert installed.exit_code == 0, installed.output
    assert json.loads(installed.output)["item"]["installed"] is True
    assert _FakeCodexCLIHost.acceptances == [True]

    removed = runner.invoke(
        plugin,
        ["--output", "json", "codex", "uninstall", "review@official"],
        env=environment,
    )
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.output)["installed"] is False
    assert set(_FakeCodexCLIHost.homes) == {tmp_path / "codex-home"}


def _dsh_cli_inventory(*, enabled: bool = True) -> DshPluginInventory:
    return DshPluginInventory(
        profile="studio",
        name="@deepseek-ai/dsh-subagent-codex",
        display_name="DSH Codex subagent",
        version="0.1.1-rc.2",
        requested_spec="0.1.1-rc.2",
        enabled=enabled,
    )


class _FakeDshCLIHost:
    homes: list[Path] = []
    profiles: list[str] = []
    accepted: list[bool] = []
    enabled = True
    installed = True

    def __init__(
        self,
        *,
        dsh_home: Path,
        profile: str,
        dsh_command: tuple[str, ...] | None,
    ) -> None:
        assert dsh_command is not None
        assert Path(dsh_command[0]).name == "dsh"
        self.host = DshBridgeHost(version="0.1.1-rc.2")
        type(self).homes.append(dsh_home)
        type(self).profiles.append(profile)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def list_plugins(self):
        return (_dsh_cli_inventory(enabled=type(self).enabled),) if type(self).installed else ()

    def get_plugin(self, name: str):
        assert name == "@deepseek-ai/dsh-subagent-codex"
        return _dsh_cli_inventory(enabled=type(self).enabled)

    def install_plugin(self, source: str, *, accept_host_permissions: bool):
        assert source == "@deepseek-ai/dsh-subagent-codex"
        type(self).accepted.append(accept_host_permissions)
        type(self).installed = True
        type(self).enabled = False
        return _dsh_cli_inventory(enabled=False)

    def set_enabled(self, name: str, *, enabled: bool):
        assert name == "@deepseek-ai/dsh-subagent-codex"
        type(self).enabled = enabled
        return _dsh_cli_inventory(enabled=enabled)

    def update_plugin(self, name: str, *, accept_host_permissions: bool):
        assert name == "@deepseek-ai/dsh-subagent-codex"
        type(self).accepted.append(accept_host_permissions)
        return _dsh_cli_inventory(enabled=type(self).enabled)

    def uninstall_plugin(self, name: str) -> None:
        assert name == "@deepseek-ai/dsh-subagent-codex"
        type(self).installed = False
        type(self).enabled = False

    def project_profile(self):
        return DshProfileProjection(
            profile="studio",
            bundles=("@deepseek-ai/dsh-base",),
            config_digest="sha256:" + "a" * 64,
            config_bytes=128,
            host_version="0.1.1-rc.2",
        )


def test_top_level_dsh_lifecycle_and_explicit_alias_are_identical(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakeDshCLIHost.homes = []
    _FakeDshCLIHost.profiles = []
    _FakeDshCLIHost.accepted = []
    _FakeDshCLIHost.enabled = True
    _FakeDshCLIHost.installed = True
    monkeypatch.setattr(
        "ksadk.plugins.bridges.dsh.DshProfilePluginBridge",
        _FakeDshCLIHost,
    )
    dsh = tmp_path / "dsh"
    dsh.write_text("#!/bin/sh\necho 'dsh 0.1.1-rc.2'\n", encoding="utf-8")
    dsh.chmod(0o755)
    environment = {
        DSH_HOME_ENV: str(tmp_path / "dsh-home"),
        DSH_BIN_ENV: str(dsh),
        DSH_PROFILE_ENV: "studio",
    }
    runner = CliRunner()

    canonical = runner.invoke(plugin, ["--output", "json", "list"], env=environment)
    alias = runner.invoke(plugin, ["--output", "json", "dsh", "list"], env=environment)
    assert canonical.exit_code == alias.exit_code == 0
    assert json.loads(canonical.output) == json.loads(alias.output)
    assert json.loads(canonical.output)["items"][0]["integrationMode"] == "bridged"

    rejected = runner.invoke(
        plugin,
        ["--output", "json", "install", "@deepseek-ai/dsh-subagent-codex"],
        env=environment,
    )
    assert rejected.exit_code == EXIT_CODE_VALIDATION
    assert json.loads(rejected.output)["error"]["code"] == (
        "dsh_plugin_permission_confirmation_required"
    )
    assert _FakeDshCLIHost.accepted == []

    installed = runner.invoke(
        plugin,
        [
            "--output",
            "json",
            "install",
            "@deepseek-ai/dsh-subagent-codex",
            "--accept-host-permissions",
        ],
        env=environment,
    )
    assert installed.exit_code == 0, installed.output
    assert json.loads(installed.output)["item"]["enabled"] is False
    assert _FakeDshCLIHost.accepted == [True]

    enabled = runner.invoke(
        plugin,
        ["--output", "json", "enable", "@deepseek-ai/dsh-subagent-codex"],
        env=environment,
    )
    assert enabled.exit_code == 0, enabled.output
    assert json.loads(enabled.output)["item"]["enabled"] is True

    disabled = runner.invoke(
        plugin,
        ["--output", "json", "disable", "@deepseek-ai/dsh-subagent-codex"],
        env=environment,
    )
    assert disabled.exit_code == 0, disabled.output
    assert json.loads(disabled.output)["item"]["enabled"] is False

    updated = runner.invoke(
        plugin,
        [
            "--output",
            "json",
            "update",
            "@deepseek-ai/dsh-subagent-codex",
            "--accept-host-permissions",
        ],
        env=environment,
    )
    assert updated.exit_code == 0, updated.output
    assert _FakeDshCLIHost.accepted == [True, True]

    projection = runner.invoke(plugin, ["--output", "json", "profile"], env=environment)
    assert projection.exit_code == 0, projection.output
    assert "config" not in json.loads(projection.output)["profile"]
    assert json.loads(projection.output)["profile"]["configDigest"].startswith("sha256:")

    removed = runner.invoke(
        plugin,
        ["--output", "json", "uninstall", "@deepseek-ai/dsh-subagent-codex"],
        env=environment,
    )
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.output)["installed"] is False
    assert set(_FakeDshCLIHost.homes) == {tmp_path / "dsh-home"}
    assert set(_FakeDshCLIHost.profiles) == {"studio"}
