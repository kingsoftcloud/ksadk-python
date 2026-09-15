from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.harness import HarnessConfig, HarnessReasoningTurn, HarnessRuntimeAdapter
from ksadk.runtime import RuntimeExecutor, RuntimeRegistry
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import AgentSpec
from ksadk.studio.service import StudioService


class _BuildLabelReasoner:
    def __init__(self, label: str) -> None:
        self.label = label

    async def complete(self, *, model, prompt, messages, tools):  # type: ignore[no-untyped-def]
        del model, prompt, tools
        return HarnessReasoningTurn(
            final_text=f"{self.label}:{messages[-1]['content']}",
            tool_calls=(),
        )


def _agent_spec(project_path: str) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "description": "Scheduler product-entry fixture",
            "runtime": {
                "type": "adk",
                "version": "fixture",
                "projectPath": project_path,
                "entryPoint": "agent:root_agent",
            },
            "instructions": {
                "system": "You are a deterministic scheduler fixture.",
                "task": "Acknowledge scheduled work.",
            },
            "model": {
                "provider": "openai-compatible",
                "model": "fixture-model",
                "endpointUrl": "https://model.example.com/v1/chat/completions",
                "credentialRef": "env://MODEL_API_KEY",
            },
            "capabilities": {"skills": [], "mcpServers": [], "tools": []},
            "execution": {
                "strategy": "direct",
                "maxSteps": 4,
                "timeoutSeconds": 30,
                "retry": {"maxAttempts": 1, "backoffSeconds": 0},
            },
            "context": {
                "maxInputTokens": 4096,
                "reserveOutputTokens": 512,
                "compaction": {"enabled": True, "thresholdRatio": 0.8},
            },
            "security": {
                "toolPolicy": "deny-by-default",
                "allowedPermissions": [],
                "network": {
                    "mode": "restricted",
                    "allowedHosts": ["model.example.com"],
                    "allowPrivateNetwork": False,
                },
            },
        }
    )


def _schedule_payload(label: str) -> dict:
    return {
        "displayName": f"Schedule {label}",
        "prompt": f"execute {label}",
        "schedule": {
            "kind": "once",
            "at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    }


def _wait_terminal(client: TestClient, agent_id: str, task_id: str) -> dict:
    items: list[dict] = []
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        items = client.get(
            f"/api/v1/agents/{agent_id}/schedules/{task_id}/occurrences"
        ).json()["items"]
        if items and items[0]["state"] in {
            "succeeded",
            "failed",
            "cancelled",
            "skipped",
        }:
            return items[0]
        time.sleep(0.05)
    raise AssertionError(
        f"scheduled task {task_id} did not reach a terminal: {items!r}"
    )


def test_product_lifespan_routes_two_agent_builds_without_manual_kernel(
    tmp_path: Path,
) -> None:
    """The shipped API lifespan owns Scheduler kernels without test patching.

    Runtime injection replaces only the external framework boundary.  The
    Studio service, scheduler registry, AgentControl Inbox, worker, canonical
    SessionEvent projection, API routes, startup and shutdown are production.
    """

    created_for: list[Path] = []
    runtime_registry = RuntimeRegistry()

    def create_adapter(context):  # type: ignore[no-untyped-def]
        created_for.append(context.project_dir)
        return HarnessRuntimeAdapter(
            HarnessConfig(model="fixture-model", prompt="fixture system"),
            agent_name=context.project_dir.name,
            reasoner=_BuildLabelReasoner(context.project_dir.name),
            workspace_root=context.project_dir,
        )

    runtime_registry.register("adk", create_adapter)
    service = StudioService(
        tmp_path,
        runtime_executor=RuntimeExecutor(runtime_registry),
    )
    for agent_id in ("agent-a", "agent-b"):
        project_path = Path("projects") / agent_id
        project_root = tmp_path / project_path
        project_root.mkdir(parents=True)
        (project_root / "agent.py").write_text(
            "from google.adk.agents import Agent\n"
            f"root_agent = Agent(name={agent_id!r}, model='fixture-model')\n",
            encoding="utf-8",
        )
        spec = _agent_spec(project_path.as_posix())
        service.create_studio_agent(
            agent_id=agent_id,
            name=agent_id,
            spec=spec,
            runtime=spec.runtime,
        )

    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        tasks: dict[str, dict] = {}
        for agent_id in ("agent-a", "agent-b"):
            response = client.post(
                f"/api/v1/agents/{agent_id}/schedules",
                json=_schedule_payload(agent_id),
            )
            assert response.status_code == 201, response.text
            tasks[agent_id] = response.json()

        assert service.scheduler_runtimes.active_runtime_count == 2
        assert len({task["target"]["agentVersionRef"] for task in tasks.values()}) == 2
        assert len({task["target"]["agentInstanceId"] for task in tasks.values()}) == 2
        # One retained capability adapter per Build; product registration does
        # not construct a second throwaway probe.
        assert len(created_for) == 2

        terminals = {}
        for agent_id, task in tasks.items():
            task_id = task["taskId"]
            accepted = client.post(
                f"/api/v1/agents/{agent_id}/schedules/{task_id}:run"
            )
            assert accepted.status_code == 202, accepted.text
        for agent_id, task in tasks.items():
            task_id = task["taskId"]
            terminals[agent_id] = _wait_terminal(client, agent_id, task_id)

        assert {item["state"] for item in terminals.values()} == {"succeeded"}
        assert len({item["sessionId"] for item in terminals.values()}) == 2
        assert len({item["runId"] for item in terminals.values()}) == 2
        assert len(created_for) == 2

    assert service.scheduler_runtimes.active_runtime_count == 0
    assert not service.scheduler_runtimes.started
