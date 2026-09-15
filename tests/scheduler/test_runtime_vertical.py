from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.kernel.bootstrap import (
    AgentKernelRuntimeConfig,
    build_agent_kernel_runtime,
    clear_agent_kernel_runtime,
    set_agent_kernel_runtime,
)
from ksadk.kernel.ingress import clear_agent_kernel, set_agent_kernel
from ksadk.scheduler import SchedulerEngine, SchedulerSQLiteStore
from ksadk.scheduler.contracts import (
    ScheduleCommandTemplate,
    ScheduledTask,
    ScheduledTaskTarget,
    ScheduleSpec,
)
from ksadk.scheduler.dispatcher import AgentControlSchedulerDispatcher
from ksadk.sessions.in_memory import InMemorySessionService


class _MutableClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self) -> None:
        self.value += timedelta(seconds=1)


def _task(
    task_id: str,
    *,
    continuity: str,
    session_id: str | None = None,
) -> ScheduledTask:
    now = datetime.now(timezone.utc)
    return ScheduledTask(
        task_id=task_id,
        target=ScheduledTaskTarget(
            tenant_id="local",
            agent_instance_id="local-agent",
            agent_version_ref="fixture-v1",
            session_id=session_id,
            authorization_ref="credential://scheduler-runtime-fixture",
        ),
        schedule=ScheduleSpec(
            kind="interval",
            every_seconds=60,
            anchor_at=now,
        ),
        command=ScheduleCommandTemplate(payload={"content": "write the report"}),
        continuity=continuity,  # type: ignore[arg-type]
        next_run_at=now + timedelta(hours=1),
    )


@asynccontextmanager
async def _running_kernel(adapter_provider):
    service = InMemorySessionService()
    runtime = build_agent_kernel_runtime(
        AgentKernelRuntimeConfig(
            agent_instance_id="local-agent",
            authority_mode="local",
            driver="memory",
            adapter_provider=adapter_provider,
            session_service=service,
            poll_interval=0.005,
            lease_ttl_seconds=5,
            activation_id="scheduler-runtime-test",
        )
    )
    clear_agent_kernel()
    clear_agent_kernel_runtime()
    set_agent_kernel(runtime.kernel)
    set_agent_kernel_runtime(runtime)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.close()
        clear_agent_kernel()
        clear_agent_kernel_runtime()


def _occurrence(store: SchedulerSQLiteStore, task_id: str, occurrence_id: str):
    return next(
        item for item in store.list_occurrences(task_id) if item.occurrence_id == occurrence_id
    )


async def _wait_for_terminal(
    engine: SchedulerEngine,
    store: SchedulerSQLiteStore,
    task_id: str,
    occurrence_id: str,
):
    for _ in range(100):
        await engine.reconcile()
        current = _occurrence(store, task_id, occurrence_id)
        if current.state in {"succeeded", "failed", "cancelled", "skipped"}:
            return current
        await asyncio.sleep(0.01)
    pytest.fail(f"scheduler occurrence {occurrence_id} did not reach a terminal state")


class _HarnessReasoner:
    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[dict[str, Any], ...]] = []

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, tools
        self.calls.append(tuple(messages))
        if self.failure is not None:
            raise RuntimeError(self.failure)
        from ksadk.harness.reasoner import HarnessReasoningTurn

        return HarnessReasoningTurn(final_text="scheduled harness result")


def _harness_provider(reasoner: _HarnessReasoner, workspace: Path):
    harness_config = pytest.importorskip(
        "ksadk.harness.config",
        reason="Harness runtime requires the optional ksadk[adk] dependencies",
        exc_type=ImportError,
    )
    harness_runtime = pytest.importorskip(
        "ksadk.harness.runtime",
        reason="Harness runtime requires the optional ksadk[adk] dependencies",
        exc_type=ImportError,
    )
    HarnessConfig = harness_config.HarnessConfig
    HarnessRuntimeAdapter = harness_runtime.HarnessRuntimeAdapter
    config = HarnessConfig(model="fixture-model", prompt="fixture system prompt")
    adapter = HarnessRuntimeAdapter(
        config,
        reasoner=reasoner,
        workspace_root=workspace,
    )

    def provide():
        # Harness continuity is explicitly process-local.  Production
        # HarnessApp likewise owns one adapter, so repeated Kernel turns must
        # resolve through that same in-process adapter instance.
        return adapter

    return provide


@pytest.mark.asyncio
async def test_new_session_runs_twice_in_distinct_kernel_sessions_and_waits_for_terminal(
    tmp_path: Path,
) -> None:
    reasoner = _HarnessReasoner()
    async with _running_kernel(_harness_provider(reasoner, tmp_path)) as runtime:
        store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
        task = _task("new-session-runtime", continuity="new_session")
        store.put_task(task)
        clock = _MutableClock()
        engine = SchedulerEngine(
            store,
            AgentControlSchedulerDispatcher(),
            owner_id="scheduler-runtime-test",
            clock=clock,
        )

        first_accepted = await engine.run_now(task.task_id)
        assert first_accepted.state == "accepted"
        first = await _wait_for_terminal(engine, store, task.task_id, first_accepted.occurrence_id)
        clock.advance()
        second_accepted = await engine.run_now(task.task_id)
        assert second_accepted.state == "accepted"
        second = await _wait_for_terminal(
            engine, store, task.task_id, second_accepted.occurrence_id
        )

        assert first.state == second.state == "succeeded"
        assert first.session_id != second.session_id
        assert first.session_id.startswith("sched-occ_")
        assert second.session_id.startswith("sched-occ_")
        assert len(reasoner.calls) == 2
        assert all(len(request) == 2 for request in reasoner.calls)
        for occurrence in (first, second):
            events = await RuntimeEventStore(runtime.session_events).list(
                occurrence.session_id,
                run_id=occurrence.run_id,
            )
            assert events[-1].event_type == "run.completed"


@pytest.mark.asyncio
async def test_continue_session_reuses_harness_session_and_supplies_prior_history(
    tmp_path: Path,
) -> None:
    reasoner = _HarnessReasoner()
    async with _running_kernel(_harness_provider(reasoner, tmp_path)) as runtime:
        store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
        task = _task(
            "continue-harness-runtime",
            continuity="continue_session",
            session_id="harness-scheduled-conversation",
        )
        store.put_task(task)
        clock = _MutableClock()
        engine = SchedulerEngine(
            store,
            AgentControlSchedulerDispatcher(),
            owner_id="scheduler-runtime-test",
            clock=clock,
        )

        first_accepted = await engine.run_now(task.task_id)
        assert first_accepted.state == "accepted"
        first = await _wait_for_terminal(engine, store, task.task_id, first_accepted.occurrence_id)
        clock.advance()
        second_accepted = await engine.run_now(task.task_id)
        assert second_accepted.state == "accepted"
        second = await _wait_for_terminal(
            engine, store, task.task_id, second_accepted.occurrence_id
        )

        assert first.state == second.state == "succeeded"
        assert first.session_id == second.session_id == "harness-scheduled-conversation"
        assert first.run_id != second.run_id
        assert reasoner.calls[1] == (
            {"role": "system", "content": "fixture system prompt"},
            {"role": "user", "content": "write the report"},
            {"role": "assistant", "content": "scheduled harness result"},
            {"role": "user", "content": "write the report"},
        )
        events = await RuntimeEventStore(runtime.session_events).list(
            "harness-scheduled-conversation"
        )
        assert [event.event_type for event in events].count("run.completed") == 2


class _CodexFixtureBackend:
    """Durable Codex service state shared by fresh per-run clients."""

    def __init__(self, *, turn_delay_seconds: float = 0) -> None:
        self.thread_count = 0
        self.turn_count = 0
        self.threads: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.turn_delay_seconds = turn_delay_seconds

    def create_thread(self) -> str:
        self.thread_count += 1
        thread_id = f"codex-thread-{self.thread_count}"
        self.threads.add(thread_id)
        self.calls.append(("thread/start", thread_id))
        return thread_id

    def create_turn(self, thread_id: str) -> str:
        self.turn_count += 1
        turn_id = f"codex-turn-{self.turn_count}"
        self.calls.append(("turn/start", thread_id))
        return turn_id


class _CodexFixtureClient:
    """Strict fixture for the real Codex adapter's start/resume/event mapping."""

    def __init__(self, backend: _CodexFixtureBackend) -> None:
        self.backend = backend
        self.attached_threads: set[str] = set()

    async def start_thread(self, config=None) -> str:
        del config
        thread_id = self.backend.create_thread()
        self.attached_threads.add(thread_id)
        return thread_id

    async def resume_thread(self, thread_id: str, config=None) -> str:
        del config
        if thread_id not in self.backend.threads:
            raise RuntimeError(f"unknown Codex thread: {thread_id}")
        self.backend.calls.append(("thread/resume", thread_id))
        self.attached_threads.add(thread_id)
        return thread_id

    def run_turn(self, thread_id: str, prompt: Any, *, config=None) -> AsyncIterator[dict]:
        del prompt, config

        async def events() -> AsyncIterator[dict]:
            if thread_id not in self.attached_threads:
                await self.resume_thread(thread_id)
            if self.backend.turn_delay_seconds:
                await asyncio.sleep(self.backend.turn_delay_seconds)
            turn_id = self.backend.create_turn(thread_id)
            turn = {
                "id": turn_id,
                "status": "inProgress",
                "items": [],
                "error": None,
            }
            yield {
                "method": "turn/started",
                "params": {"threadId": thread_id, "turn": turn},
            }
            yield {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {**turn, "status": "completed"},
                },
            }

        return events()

    async def close(self) -> None:
        self.attached_threads.clear()


@pytest.mark.asyncio
async def test_continue_session_resumes_one_native_codex_thread_across_fresh_adapters(
    tmp_path: Path,
) -> None:
    backend = _CodexFixtureBackend()

    def provide() -> CodexRuntimeAdapter:
        return CodexRuntimeAdapter(_CodexFixtureClient(backend))  # type: ignore[arg-type]

    async with _running_kernel(provide) as runtime:
        store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
        task = _task(
            "continue-session-runtime",
            continuity="continue_session",
            session_id="scheduled-conversation",
        )
        store.put_task(task)
        clock = _MutableClock()
        engine = SchedulerEngine(
            store,
            AgentControlSchedulerDispatcher(),
            owner_id="scheduler-runtime-test",
            clock=clock,
        )

        first_accepted = await engine.run_now(task.task_id)
        assert first_accepted.state == "accepted"
        first = await _wait_for_terminal(engine, store, task.task_id, first_accepted.occurrence_id)
        clock.advance()
        second_accepted = await engine.run_now(task.task_id)
        assert second_accepted.state == "accepted"
        second = await _wait_for_terminal(
            engine, store, task.task_id, second_accepted.occurrence_id
        )

        assert first.state == second.state == "succeeded"
        assert first.session_id == second.session_id == "scheduled-conversation"
        assert first.run_id != second.run_id
        assert backend.calls == [
            ("thread/start", "codex-thread-1"),
            ("turn/start", "codex-thread-1"),
            ("thread/resume", "codex-thread-1"),
            ("turn/start", "codex-thread-1"),
        ]
        events = await RuntimeEventStore(runtime.session_events).list("scheduled-conversation")
        assert [event.event_type for event in events].count("continuation.created") == 1
        assert [event.event_type for event in events].count("run.completed") == 2


@pytest.mark.asyncio
async def test_codex_timeout_is_a_typed_terminal_not_a_stuck_accepted_occurrence(
    tmp_path: Path,
) -> None:
    backend = _CodexFixtureBackend(turn_delay_seconds=0.1)

    def provide() -> CodexRuntimeAdapter:
        return CodexRuntimeAdapter(  # type: ignore[arg-type]
            _CodexFixtureClient(backend),
            turn_timeout_seconds=0.01,
        )

    async with _running_kernel(provide) as runtime:
        store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
        task = _task("codex-timeout-runtime", continuity="new_session")
        store.put_task(task)
        engine = SchedulerEngine(
            store,
            AgentControlSchedulerDispatcher(),
            owner_id="scheduler-runtime-test",
            clock=_MutableClock(),
        )

        accepted = await engine.run_now(task.task_id)
        assert accepted.state == "accepted"
        failed = await _wait_for_terminal(engine, store, task.task_id, accepted.occurrence_id)

        assert failed.state == "failed"
        assert failed.error_code == "codex_runtime_failed"
        assert failed.detail == "codex turn timed out"
        assert [transition.state for transition in failed.transitions] == [
            "claimed",
            "accepted",
            "running",
            "failed",
        ]
        events = await RuntimeEventStore(runtime.session_events).list(
            failed.session_id,
            run_id=failed.run_id,
        )
        assert events[-1].event_type == "run.failed"


@pytest.mark.asyncio
async def test_runtime_failure_is_a_typed_terminal_not_an_accepted_success(
    tmp_path: Path,
) -> None:
    reasoner = _HarnessReasoner(failure="fixture reasoner exploded")
    async with _running_kernel(_harness_provider(reasoner, tmp_path)) as runtime:
        store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
        task = _task("failed-runtime", continuity="new_session")
        store.put_task(task)
        engine = SchedulerEngine(
            store,
            AgentControlSchedulerDispatcher(),
            owner_id="scheduler-runtime-test",
            clock=_MutableClock(),
        )

        accepted = await engine.run_now(task.task_id)
        assert accepted.state == "accepted"
        failed = await _wait_for_terminal(engine, store, task.task_id, accepted.occurrence_id)

        assert failed.state == "failed"
        assert failed.error_code == "HARNESS_EXECUTION_FAILED"
        assert failed.detail == "fixture reasoner exploded"
        assert failed.completed_at is not None
        events = await RuntimeEventStore(runtime.session_events).list(
            failed.session_id,
            run_id=failed.run_id,
        )
        assert events[-1].event_type == "run.failed"
