"""Wheel-owned Teams bundle graph installed by the official DSH Profile bridge."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.companions import DshCompanionDefinition
from ksadk.plugins.dsh_home import prepare_studio_dsh_home

TEAMS_PLUGIN_ID = "io.ksadk.teams"
TEAMS_PLUGIN_VERSION = "0.1.0"
TEAMS_COMPONENTS = {
    f"dsh-teams-{part}": f"@kingsoftcloud/dsh-teams-{part}"
    for part in ("profile", "tasks", "router", "tools", "leader-policy", "client")
}
TEAMS_OPERATIONS = frozenset(
    {
        "team_context",
        "team_message",
        "team_create_task",
        "team_task_action",
        "team_wait",
        "team_submit_result",
        "team_finish",
        "team_publish_artifact",
        "team_read_artifact",
    }
)


def teams_companion_definition(application: Any) -> DshCompanionDefinition:
    root = Path(__file__).parent
    sources = (
        *sorted((root / "teams").glob("*.py")),
        root / "companions.py",
        root / "companion_artifacts.py",
        root / "dsh_teams.py",
        root / "providers" / "dsh_capabilities.py",
        root / "providers" / "bundles" / "ksadk-dsh-companion-host" / "index.mjs",
        root / "providers" / "bundles" / "ksadk-dsh-companion-host" / "client.mjs",
    )
    return DshCompanionDefinition(
        plugin_id=TEAMS_PLUGIN_ID,
        components=TEAMS_COMPONENTS,
        operations=TEAMS_OPERATIONS,
        start=application.start,
        revoke=application.revoke,
        close=application.close,
        invoke=application.invoke,
        profile="web",
        bind_artifact=getattr(application, "bind_artifact", None),
        artifact_sources=sources,
    )


def configure_teams_profile(
    *,
    workspace: Path,
    dsh_home: Path,
    dsh_command: Sequence[str],
    enabled: bool,
) -> dict[str, Any]:
    """Apply all six enabled flags atomically after the caller enters maintenance.

    The existing bridge owns the Profile lock, immutable sources, validation,
    pnpm graph and rollback. A single outer transaction covers the stack;
    individual DSH operations remain the same validated operations as the UI.
    Installation is explicit, and this function never opens a second Core.
    """
    prepare_studio_dsh_home(dsh_home)
    with DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile="web",
        dsh_command=dsh_command,
        cwd=workspace,
    ) as bridge:
        with bridge._profile_transaction(exclusive=True):
            snapshot = bridge._snapshot()
            try:
                installed = {item.name: item for item in bridge.list_plugins()}
                if enabled:
                    for component, package in TEAMS_COMPONENTS.items():
                        current = installed.get(package)
                        if current is None:
                            root = Path(__file__).parent / "providers" / "bundles" / component
                            current = bridge.install_plugin(str(root), accept_host_permissions=True)
                        if current.name != package or current.version != TEAMS_PLUGIN_VERSION:
                            raise ValueError("unsupported Teams companion bundle")
                        bridge.set_enabled(package, enabled=True)
                else:
                    for package in reversed(tuple(TEAMS_COMPONENTS.values())):
                        if package in installed:
                            bridge.set_enabled(package, enabled=False)
                projection = bridge.project_profile()
                return {
                    "profile": "web",
                    "enabled": enabled,
                    "profileDigest": projection.config_digest,
                }
            except BaseException as error:
                bridge._rollback(snapshot, error)
                raise
