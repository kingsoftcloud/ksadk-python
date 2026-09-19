"""Wheel-owned DSH bundle for the Studio Channel workspace page."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.dsh_home import prepare_studio_dsh_home

CHANNEL_PLUGIN_PACKAGE = "@kingsoftcloud/dsh-channels-client"
CHANNEL_PLUGIN_VERSION = "0.1.0"
_MARKER_NAME = ".ksadk-channel-default.json"


def _marker_path(dsh_home: Path) -> Path:
    return dsh_home / "profiles" / "web" / _MARKER_NAME


def _read_marker(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_marker(path: Path, *, disabled: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"schemaVersion": 1, "defaultApplied": True, "defaultDisabled": disabled},
                stream,
            )
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def channel_default_bootstrap_needed(dsh_home: Path) -> bool:
    """Return whether the Studio-owned default has not reached a terminal state."""
    marker = _read_marker(_marker_path(dsh_home))
    return not marker.get("defaultApplied") and not marker.get("defaultDisabled")


def channel_workspace_contributions(
    dsh_home: Path, profile_name: str = "web"
) -> list[dict[str, Any]]:
    """Read enabled client contributions without starting DSH Core."""
    profile_root = dsh_home / "profiles" / profile_name
    try:
        profile = json.loads((profile_root / "package.json").read_text(encoding="utf-8"))
        config = profile.get("dsh", {}).get("profile", {})
        bundles = config.get("bundles", [])
        package_path = (
            profile_root
            / "node_modules"
            / "@kingsoftcloud"
            / "dsh-channels-client"
            / "package.json"
        )
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return []
    if (
        not isinstance(bundles, list)
        or not isinstance(package, dict)
        or CHANNEL_PLUGIN_PACKAGE not in bundles
        or package.get("name") != CHANNEL_PLUGIN_PACKAGE
    ):
        return []
    return [
        {"id": "channels", "label": "消息渠道", "pluginId": CHANNEL_PLUGIN_PACKAGE, "order": 45}
    ]


def configure_channel_profile(
    *,
    workspace: Path,
    dsh_home: Path,
    dsh_command: Sequence[str],
    enabled: bool = True,
) -> dict[str, Any]:
    """Install/enable the Studio Channel UI in one validated Profile transaction.

    This helper intentionally owns only the client bundle.  The channel API and
    connector remain Studio/SDK responsibilities, so loading the DSH bundle does
    not create a second channel service or Core.
    """
    prepare_studio_dsh_home(dsh_home)
    marker = _marker_path(dsh_home)
    marker_payload = _read_marker(marker)
    if enabled and marker_payload.get("defaultDisabled") is True:
        return {"profile": "web", "enabled": False, "skipped": "explicitly_disabled"}
    with DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile="web",
        dsh_command=dsh_command,
        cwd=workspace,
    ) as bridge:
        result: dict[str, Any] | None = None
        with bridge._profile_transaction(exclusive=True):
            snapshot = bridge._snapshot()
            try:
                installed = {item.name: item for item in bridge.list_plugins()}
                current = installed.get(CHANNEL_PLUGIN_PACKAGE)
                if enabled:
                    # A user-disabled or uninstalled bundle is an explicit
                    # choice; the default bootstrap must never undo it.
                    if current is None and marker_payload.get("defaultApplied") is True:
                        return {"profile": "web", "enabled": False, "skipped": "explicitly_removed"}
                    if current is not None and not current.enabled:
                        _write_marker(marker, disabled=True)
                        return {
                            "profile": "web",
                            "enabled": False,
                            "skipped": "explicitly_disabled",
                        }
                    if current is not None and current.enabled:
                        _write_marker(marker, disabled=False)
                        return {"profile": "web", "enabled": True, "skipped": "already_enabled"}
                    if current is None:
                        source = (
                            Path(__file__).parent / "providers" / "bundles" / "dsh-channels-client"
                        )
                        current = bridge.install_plugin(str(source), accept_host_permissions=True)
                    if (
                        current.name != CHANNEL_PLUGIN_PACKAGE
                        or current.version != CHANNEL_PLUGIN_VERSION
                    ):
                        raise ValueError("unsupported Channel client bundle")
                    bridge.set_enabled(CHANNEL_PLUGIN_PACKAGE, enabled=True)
                elif current is not None:
                    bridge.set_enabled(CHANNEL_PLUGIN_PACKAGE, enabled=False)
                projection = bridge.project_profile()
                result = {
                    "profile": "web",
                    "enabled": enabled,
                    "profileDigest": projection.config_digest,
                }
            except BaseException as error:
                bridge._rollback(snapshot, error)
                raise
        _write_marker(marker, disabled=not enabled)
        return result or {"profile": "web", "enabled": enabled}
