from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ksadk.events.session_event import session_event_to_envelope
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
from ksadk.scheduler.contracts import ScheduleSpec
from ksadk.studio.contracts import AgentSpec
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService


class _Reasoner:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], ...]] = []

    async def complete(self, *, model, prompt, messages, tools):  # type: ignore[no-untyped-def]
        del model, prompt, tools
        self.calls.append(tuple(messages))
        return HarnessReasoningTurn(final_text="scheduled product result")


class _UsageReasoner(_Reasoner):
    """Reasoner that reports per-turn usage the way the real model client does."""

    async def complete(self, *, model, prompt, messages, tools):  # type: ignore[no-untyped-def]
        del model, prompt, tools
        self.calls.append(tuple(messages))
        return HarnessReasoningTurn(
            final_text="scheduled product result",
            usage={
                "input_tokens": 210,
                "output_tokens": 33,
                "cached_tokens": 12,
                "reasoning_tokens": 9,
            },
        )


def _install_managed_harness_profile(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = root / ".agentkit" / "dsh-home"
    profile = home / "profiles" / "studio"
    installed = profile / "node_modules" / "@kingsoftcloud" / "ksadk-harness-provider"
    installed.parent.mkdir(parents=True)
    shutil.copytree(shipped_harness_dsh_bundle().root, installed)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"@kingsoftcloud/ksadk-harness-provider": "1.0.0"},
                "dsh": {
                    "profile": {
                        "bundles": ["@kingsoftcloud/ksadk-harness-provider"]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    executable = root / ".agentkit" / "dsh-fixture"
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--version*) echo 0.1.1-rc.2;;\n"
        "  *--dump-config*) echo 'profile: studio; harness: 1.0.0';;\n"
        "  *) exit 2;;\n"
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("KSADK_DSH_HOME", str(home))
    monkeypatch.setenv("KSADK_DSH_PROFILE", "studio")
    monkeypatch.setenv("KSADK_DSH_BIN", str(executable))
    monkeypatch.setenv("KSADK_AGENT_KERNEL", "1")


def _spec(*, allow_host_process: bool = True) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "description": "Scheduler managed DSH Harness fixture",
            "runtime": {"type": "harness"},
            "instructions": {
                "system": "You are the managed DSH Harness fixture.",
                "task": "Complete scheduled work.",
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
                "allowedPermissions": (
                    ["process:host-user"] if allow_host_process else []
                ),
                "network": {
                    "mode": "restricted",
                    "allowedHosts": ["model.example.com"],
                    "allowPrivateNetwork": False,
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_scheduler_settles_real_managed_dsh_harness_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the product service through DSH discovery, Kernel and terminal facts."""

    _install_managed_harness_profile(tmp_path, monkeypatch)
    reasoner = _Reasoner()
    service = StudioService(tmp_path, harness_reasoner=reasoner)
    spec = _spec()
    service.create_studio_agent(
        agent_id="scheduler-dsh-harness",
        name="Scheduler DSH Harness",
        description=spec.description,
        spec=spec,
        runtime=spec.runtime,
    )
    await service.start()
    try:
        task = await service.create_agent_schedule(
            "scheduler-dsh-harness",
            display_name="Managed DSH Harness",
            prompt="execute managed DSH harness",
            schedule=ScheduleSpec(kind="interval", every_seconds=3600),
        )
        accepted = await service.run_agent_schedule_now(
            "scheduler-dsh-harness", task.task_id
        )
        current = accepted
        for _ in range(200):
            await service.scheduler.engine.reconcile()
            current = service.scheduler.list_occurrences(task.task_id)[0]
            if current.state in {"succeeded", "failed", "cancelled", "skipped"}:
                break
            await asyncio.sleep(0.01)

        if current.state not in {"succeeded", "failed", "cancelled", "skipped"}:
            stored = await service.session_service.get_events(current.session_id)
            envelopes = [
                envelope
                for item in stored
                if (envelope := session_event_to_envelope(item)) is not None
            ]
            event_summary = [
                (item.seq, item.event_type, item.run_id, item.causation_id)
                for item in envelopes
            ]
            pytest.fail(
                "managed DSH Harness occurrence did not settle: "
                f"{current.model_dump(mode='json', by_alias=True)!r}; "
                f"events={event_summary!r}"
            )

        assert current.state == "succeeded", current
        assert current.run_id
        assert len(reasoner.calls) == 1
    finally:
        await service.scheduler.stop()
        await service.aclose()


@pytest.mark.asyncio
async def test_managed_dsh_harness_reports_nonzero_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Engine 链路必须把 reasoner 提供的用量上报为非零 usage.reported 事件。"""

    _install_managed_harness_profile(tmp_path, monkeypatch)
    reasoner = _UsageReasoner()
    service = StudioService(tmp_path, harness_reasoner=reasoner)
    spec = _spec()
    service.create_studio_agent(
        agent_id="scheduler-dsh-usage",
        name="Scheduler DSH Usage",
        description=spec.description,
        spec=spec,
        runtime=spec.runtime,
    )
    await service.start()
    try:
        task = await service.create_agent_schedule(
            "scheduler-dsh-usage",
            display_name="Managed DSH Usage",
            prompt="execute managed DSH harness usage",
            schedule=ScheduleSpec(kind="interval", every_seconds=3600),
        )
        accepted = await service.run_agent_schedule_now(
            "scheduler-dsh-usage", task.task_id
        )
        current = accepted
        for _ in range(200):
            await service.scheduler.engine.reconcile()
            current = service.scheduler.list_occurrences(task.task_id)[0]
            if current.state in {"succeeded", "failed", "cancelled", "skipped"}:
                break
            await asyncio.sleep(0.01)

        assert current.state == "succeeded", current
        stored = await service.session_service.get_events(current.session_id)
        usage_events = [
            event
            for event in stored
            if event.event_type == "usage.reported"
        ]
        assert usage_events, "usage.reported event missing from session events"
        reported = usage_events[-1]
        payload = reported.content["session_event"]["payload"]
        assert payload.get("input_tokens") == 210, payload
        assert payload.get("output_tokens") == 33, payload
        assert payload.get("cached_tokens") == 12, payload
        assert payload.get("reasoning_tokens") == 9, payload
    finally:
        await service.scheduler.stop()
        await service.aclose()


@pytest.mark.asyncio
async def test_scheduler_rejects_unapproved_dsh_provider_before_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission failure produces no task, Inbox command, Run, or retry storm."""

    _install_managed_harness_profile(tmp_path, monkeypatch)
    service = StudioService(tmp_path, harness_reasoner=_Reasoner())
    spec = _spec(allow_host_process=False)
    service.create_studio_agent(
        agent_id="scheduler-dsh-denied",
        name="Scheduler DSH Denied",
        description=spec.description,
        spec=spec,
        runtime=spec.runtime,
    )
    await service.start()
    try:
        with pytest.raises(StudioError) as denied:
            await service.create_agent_schedule(
                "scheduler-dsh-denied",
                display_name="Denied DSH Harness",
                prompt="must not enter Inbox",
                schedule=ScheduleSpec(kind="interval", every_seconds=3600),
            )
        assert denied.value.code == "PLUGIN_PERMISSION_DENIED"
        details = denied.value.details
        assert details["reason"] == "plugin_permission_denied"
        assert "process:host-user" in details["missingPermissions"]
        assert "重新构建" in details["hint"]
        assert service.scheduler.list_tasks() == []
        assert service.scheduler.list_all_occurrences() == []
        assert service.scheduler_runtimes.active_runtime_count == 0
        assert await service.session_service.list_sessions("scheduler-dsh-denied") == []
    finally:
        await service.scheduler.stop()
        await service.aclose()
