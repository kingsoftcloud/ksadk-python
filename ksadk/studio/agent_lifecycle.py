"""Recoverable deletion for generic Studio Agents."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ksadk.studio.contracts import RunStatus
from ksadk.studio.errors import StudioError


def delete_framework_agent(studio: Any, agent_id: str, *, purge: bool) -> None:
    studio.drafts.get(agent_id)
    running = [
        run.id
        for run in studio.event_store.list_runs(agent_id=agent_id)
        if run.status in {RunStatus.CREATED, RunStatus.RUNNING}
    ]
    if running:
        raise StudioError(
            "AGENT_RUN_ACTIVE",
            "Agent 仍有运行中的任务，请等待运行结束后再删除",
            status_code=409,
            details={"agentId": agent_id, "runIds": running},
        )
    builds = studio.builds.list_for_agent(agent_id)
    removed_bindings = studio.plugin_compositions.unbind_builds(builds)
    trash_directory = None
    try:
        if not purge:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            trash_directory = studio.workspace.resolve(
                Path(".agentkit/trash/agents") / f"{agent_id}-{timestamp}"
            )
            trash_directory.mkdir(parents=True, exist_ok=False)
        studio.event_store.delete_agent(
            agent_id,
            purge=purge,
            trash_directory=trash_directory,
        )
        studio.builds.delete_for_agent(
            agent_id,
            purge=purge,
            trash_directory=trash_directory,
        )
        studio.drafts.delete(
            agent_id,
            purge=purge,
            trash_directory=trash_directory,
        )
    except Exception:
        studio.plugin_compositions.restore_bindings(removed_bindings)
        raise


__all__ = ["delete_framework_agent"]
