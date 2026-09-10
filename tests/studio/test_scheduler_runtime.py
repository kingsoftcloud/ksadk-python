from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ksadk.events.canonical import ItemCompleted
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.harness import HarnessConfig, HarnessReasoningTurn, HarnessRuntimeAdapter
from ksadk.kernel.ingress import get_agent_kernel
from ksadk.runtime import RuntimeLaunchContext
from ksadk.scheduler.contracts import (
    ScheduleCommandTemplate,
    ScheduledTask,
    ScheduledTaskTarget,
    ScheduleSpec,
)
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.scheduler_runtime import (
    StudioScheduledKernelRegistry,
    StudioSchedulerRuntimeError,
)
from ksadk.studio.scheduler_service import StudioSchedulerService
from ksadk.studio.workspace import Workspace


class _Reasoner:
    def __init__(self, label: str) -> None:
        self.label = label

    async def complete(self, *, model, prompt, messages, tools):  # type: ignore[no-untyped-def]
        del model, prompt, tools
        return HarnessReasoningTurn(
            final_text=f"{self.label}:{messages[-1]['content']}",
            tool_calls=(),
        )


def _spec(root: Path, build_id: str, agent_id: str) -> StudioRunSpec:
    return StudioRunSpec(
        launch_context=RuntimeLaunchContext(
            runtime_type="harness",
            project_dir=root / build_id,
        ),
        build_id=build_id,
        agent_id=agent_id,
        model="fixture-model",
        request_config={"prompt": f"system:{agent_id}"},
        manifest_sha256=f"sha256:{build_id}",
    )


def _task(
    task_id: str,
    *,
    target,
    content: str,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        target=ScheduledTaskTarget(
            agent_id=target.agent_id,
            tenant_id=target.tenant_id,
            agent_instance_id=target.agent_instance_id,
            agent_version_ref=target.build_id,
            authorization_ref="runtime://studio-scheduler-kernel",
        ),
        schedule=ScheduleSpec(
            kind="once",
            at=datetime.now(timezone.utc) + timedelta(days=1),
        ),
        command=ScheduleCommandTemplate(payload={"content": content}),
    )


async def _wait_terminal(
    service: StudioSchedulerService,
    task_id: str,
    occurrence_id: str,
):
    for _ in range(200):
        await service.engine.reconcile()
        current = next(
            item
            for item in service.list_occurrences(task_id)
            if item.occurrence_id == occurrence_id
        )
        if current.state in {"succeeded", "failed", "cancelled", "skipped"}:
            return current
        await asyncio.sleep(0.01)
    pytest.fail(f"occurrence {occurrence_id} did not settle")


@pytest.mark.asyncio
async def test_studio_scheduler_starts_build_pinned_kernels_without_global_ingress(
    tmp_path: Path,
) -> None:
    specs = {
        "build-a": _spec(tmp_path, "build-a", "agent-a"),
        "build-b": _spec(tmp_path, "build-b", "agent-b"),
    }
    sessions = InMemorySessionService()
    factory_calls = 0

    def provider(spec: StudioRunSpec):
        def create_adapter() -> HarnessRuntimeAdapter:
            nonlocal factory_calls
            factory_calls += 1
            return HarnessRuntimeAdapter(
                HarnessConfig(
                    model="fixture-model",
                    prompt=str(spec.request_config["prompt"]),
                ),
                agent_name=spec.agent_id,
                reasoner=_Reasoner(spec.build_id),
                workspace_root=tmp_path,
            )

        return create_adapter

    global_before = get_agent_kernel()
    registry = StudioScheduledKernelRegistry(
        resolve_build=specs.__getitem__,
        resolve_adapter_provider=provider,
        session_service=sessions,
        poll_interval=0.005,
        lease_ttl_seconds=5,
    )
    workspace = Workspace(tmp_path)
    workspace.initialize()
    scheduler = StudioSchedulerService(workspace, runtime_registry=registry)
    await scheduler.start_if_available()
    try:
        target_a = await registry.ensure_build("build-a", expected_agent_id="agent-a")
        target_b = await registry.ensure_build("build-b", expected_agent_id="agent-b")
        # Registration snapshots capabilities with one adapter per Build.  The
        # same retained instances are consumed by the first workers; no second
        # discarded probe is created at either boundary.
        assert factory_calls == 2
        scheduler.create_task(_task("task-agent-a", target=target_a, content="alpha"))
        scheduler.create_task(_task("task-agent-b", target=target_b, content="beta"))

        first = await scheduler.run_now("task-agent-a")
        second = await scheduler.run_now("task-agent-b")
        first = await _wait_terminal(scheduler, "task-agent-a", first.occurrence_id)
        second = await _wait_terminal(scheduler, "task-agent-b", second.occurrence_id)

        assert first.state == "succeeded", first
        assert second.state == "succeeded", second
        assert first.run_id and second.run_id and first.run_id != second.run_id
        assert registry.active_runtime_count == 2
        assert factory_calls == 2
        assert target_a.agent_instance_id != target_b.agent_instance_id
        assert get_agent_kernel() is global_before

        first_events = await RuntimeEventStore(sessions).list(first.session_id)
        second_events = await RuntimeEventStore(sessions).list(second.session_id)
        first_text = [
            "".join(str(getattr(part, "text", "")) for part in event.snapshot.parts)
            for event in first_events
            if isinstance(event, ItemCompleted) and event.item_kind == "message"
        ]
        second_text = [
            "".join(str(getattr(part, "text", "")) for part in event.snapshot.parts)
            for event in second_events
            if isinstance(event, ItemCompleted) and event.item_kind == "message"
        ]
        assert first_text == ["build-a:alpha"]
        assert second_text == ["build-b:beta"]
    finally:
        await scheduler.stop()

    assert registry.active_runtime_count == 0
    assert not registry.started
    assert get_agent_kernel() is global_before


@pytest.mark.asyncio
async def test_scheduler_registry_rejects_a_stale_or_cross_agent_target(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "build-a", "agent-a")
    registry = StudioScheduledKernelRegistry(
        resolve_build=lambda _build_id: spec,
        resolve_adapter_provider=lambda _spec: lambda: HarnessRuntimeAdapter(
            HarnessConfig(model="fixture-model", prompt="system"),
            reasoner=_Reasoner("build-a"),
            workspace_root=tmp_path,
        ),
        session_service=InMemorySessionService(),
    )
    await registry.start()
    try:
        with pytest.raises(StudioSchedulerRuntimeError) as mismatch:
            await registry.ensure_build("build-a", expected_agent_id="agent-b")
        assert mismatch.value.code == "SCHEDULER_AGENT_MISMATCH"

        target = await registry.ensure_build("build-a", expected_agent_id="agent-a")
        with pytest.raises(StudioSchedulerRuntimeError) as stale:
            await registry.ensure_target(
                ScheduledTaskTarget(
                    agent_id="agent-a",
                    tenant_id=target.tenant_id,
                    agent_instance_id="wrong-instance",
                    agent_version_ref="build-a",
                    authorization_ref="runtime://studio-scheduler-kernel",
                )
            )
        assert stale.value.code == "SCHEDULER_TARGET_MISMATCH"
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_managed_harness_scheduler_restores_history_after_registry_restart(
    tmp_path: Path,
) -> None:
    from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
    from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
    from ksadk.sessions.local_service import LocalSessionService

    requests: list[list[dict]] = []

    class RecordingReasoner:
        async def complete(self, *, model, prompt, messages, tools, max_output_tokens=None):
            requests.append([dict(message) for message in messages])
            return HarnessReasoningTurn(final_text=f"answer-{len(requests)}")

    spec = _spec(tmp_path, "build-a", "agent-a")
    harness_spec = HarnessSpec(
        agent_revision_ref="agent-revision://agent-a@1",
        model=ModelBinding(profile_ref="model-profile://fixture@1"),
        prompt=PromptSpec(instructions="system-role"),
    )
    workspace = Workspace(tmp_path)
    workspace.initialize()

    async def start_scheduler():
        registry = StudioScheduledKernelRegistry(
            resolve_build=lambda _build: spec,
            resolve_adapter_provider=lambda _spec: lambda: ManagedHarnessRuntimeAdapter(
                harness_spec, reasoner=RecordingReasoner(), workspace_root=tmp_path,
            ),
            session_service=LocalSessionService(project_dir=str(tmp_path)),
            state_dir=tmp_path / "scheduler-state",
            poll_interval=0.005,
            lease_ttl_seconds=5,
        )
        scheduler = StudioSchedulerService(workspace, runtime_registry=registry)
        await scheduler.start_if_available()
        return scheduler, registry

    scheduler, registry = await start_scheduler()
    try:
        target = await registry.ensure_build("build-a", expected_agent_id="agent-a")
        task = _task("continued", target=target, content="remember alpha")
        task = task.model_copy(update={
            "continuity": "continue_session",
            "target": task.target.model_copy(
                update={"session_id": "continued-session"},
            ),
        })
        scheduler.create_task(task)
        first = await scheduler.run_now(task.task_id)
        first = await _wait_terminal(scheduler, task.task_id, first.occurrence_id)
        assert first.state == "succeeded", first
    finally:
        await scheduler.stop()

    # A fresh registry, adapter, and SessionService must recover from durable facts.
    scheduler, registry = await start_scheduler()
    try:
        second = await scheduler.run_now("continued")
        second = await _wait_terminal(scheduler, "continued", second.occurrence_id)
        assert second.state == "succeeded", second
        assert second.session_id == first.session_id == "continued-session"
        assert second.run_id != first.run_id
        assert [(m["role"], m["content"]) for m in requests[1]] == [
            ("system", "system-role"),
            ("user", "remember alpha"),
            ("assistant", "answer-1"),
            ("user", "remember alpha"),
        ]
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        None, "command_agent", "command_session", "command_tenant",
        "run_agent", "run_session", "failed",
    ],
)
async def test_harness_history_scopes_durable_facts_and_deduplicates_completion(mismatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from ksadk.events.canonical import RunCompleted, SourceRef
    from ksadk.events.content import ContentSnapshot, TextContent
    from ksadk.kernel.contracts import AgentControlCommand, ControlSource
    from ksadk.kernel.state import RunState
    from ksadk.kernel.store import RunRecord
    from ksadk.kernel.worker_history import harness_conversation_messages

    command = AgentControlCommand(
        command_id=uuid4(), idempotency_key="current", tenant_id="tenant-a",
        agent_instance_id="agent-a", session_id="session-a", command_type="enqueue",
        payload={"content": "current"}, source=ControlSource(kind="scheduler", ref="task-a"),
        authorization_ref="fixture", submitted_at=datetime.now(timezone.utc).isoformat(),
    )
    previous = command.model_copy(update={
        "command_id": uuid4(), "payload": {"content": "previous"},
    })
    command_mismatches = {
        "command_agent": {"agent_instance_id": "agent-b"},
        "command_session": {"session_id": "session-b"},
        "command_tenant": {"tenant_id": "tenant-b"},
    }
    previous = previous.model_copy(update=command_mismatches.get(mismatch, {}))
    run = RunRecord(
        run_id="prior-run", agent_instance_id="agent-b" if mismatch == "run_agent" else "agent-a",
        session_id="session-b" if mismatch == "run_session" else "session-a",
        state=RunState.FAILED if mismatch == "failed" else RunState.COMPLETED,
        metadata={"command_id": str(previous.command_id)},
    )
    store = SimpleNamespace(
        list_messages=AsyncMock(return_value=[SimpleNamespace(command=previous)]),
        load_run=AsyncMock(return_value=run),
    )
    sessions = InMemorySessionService()
    await sessions.create_session("agent-a", "user-a", "session-a")
    event_store = RuntimeEventStore(sessions)
    envelope = dict(
        schema_version=2, seq=0, timestamp=1.0, run_id="prior-run", scope_id="root",
        source=SourceRef(framework="langgraph"),
    )
    # Distinct canonical items remain separate messages, even with one role.
    for index in range(2):
        await event_store.append_one("session-a", ItemCompleted(
            **envelope, event_id=f"message-{index}", item_id=f"item-{index}", item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="text", text=f"answer-{index}"),)),
        ))
    # A duplicate terminal fact must not replay the same complete turn twice.
    for index in range(2):
        await event_store.append_one("session-a", RunCompleted(
            **envelope, event_id=f"completed-{index}", status="completed", output_refs=(),
        ))
    messages = await harness_conversation_messages(command, store=store, session_events=sessions)
    expected = [] if mismatch else [
        {"role": "user", "content": "previous"},
        {"role": "assistant", "content": "answer-0"},
        {"role": "assistant", "content": "answer-1"},
    ]
    assert messages == [*expected, {"role": "user", "content": "current"}]
    store.list_messages.assert_awaited_once_with("agent-a", "session-a")
