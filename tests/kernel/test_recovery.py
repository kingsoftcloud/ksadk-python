"""Task 7: fenced RuntimeEvent 写入、冷 attach/resume 与确定性收口（决策表 + 崩溃窗口）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ksadk.events.canonical import (
    RunCompleted,
    RunProgress,
    RunStarted,
    SourceRef,
)
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.pipeline import CanonicalEventPipeline
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import (
    ActivationLease,
    ActivationWriteGuard,
    AgentControlCommand,
    ControlSource,
    RuntimeCapability,
    RuntimeCapabilityMatrix,
)
from ksadk.kernel.errors import StaleFenceError
from ksadk.kernel.memory_store import InMemoryAgentKernelStore
from ksadk.kernel.recovery import RecoveryCoordinator, RecoveryReport
from ksadk.kernel.state import RunState, is_terminal_run
from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    CheckpointDescriptor,
    RunHandle,
    RuntimeAdapter,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.executor import RuntimeExecutor, handle_digest
from ksadk.runtime.launch import RuntimeLaunchContext
from ksadk.sessions.in_memory import InMemorySessionService

AGENT = "agent-instance-1"
SESSION = "session-1"
TERMINAL_RUNTIME_EVENT_TYPES = frozenset(
    {"run.completed", "run.failed", "run.canceled", "run.interrupted"}
)


class CrashPoint(Exception):
    """模拟进程在某个崩溃窗口退出。"""


def _src() -> SourceRef:
    return SourceRef(framework="ksadk")


def _capability(supported: bool, reason: str | None = None) -> RuntimeCapability:
    if supported:
        return RuntimeCapability(supported=True, mode="native")
    return RuntimeCapability(
        supported=False, mode="unavailable", reason=reason or "not_implemented"
    )


class _FakeRuntime(BaseRuntime):
    runtime_type = "fake"

    def native_capabilities(self) -> dict[str, Any]:
        return {"fake": True}


class _AttachableAdapter(RuntimeAdapter):
    """可跨进程 attach/resume 的假 runtime：状态挂在进程外字典上模拟 durable。"""

    def __init__(self, durable_state: dict[str, RunHandle]) -> None:
        super().__init__(_FakeRuntime())
        self._durable = durable_state
        self.attach_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []

    async def start(self, request: StartRequest) -> RunHandle:
        handle = RunHandle(
            run_id=str(request.metadata.get("run_id") or uuid4()),
            session_id=request.session_id,
            runtime_type="fake",
        )
        self._durable[handle.run_id] = handle
        return handle

    def stream(self, handle: RunHandle):
        async def _gen():
            yield RunStarted(
                schema_version=2,
                event_id=f"{handle.run_id}-started",
                seq=0,
                timestamp=1.0,
                run_id=handle.run_id,
                scope_id=f"run:{handle.run_id}",
                status="running",
                source=_src(),
            )

        return _gen()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return CancelResult.NOT_RUNNING

    async def resume(self, handle, target, payload) -> RunHandle:
        self.resume_calls.append((handle.run_id, target.id))
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id="ck-1",
            invocation_id=handle.run_id,
            capability=self._durable_capability(),
        )

    @staticmethod
    def _durable_capability():
        from ksadk.runtime.adapter import CheckpointCapability

        return CheckpointCapability(
            supported=True,
            granularity="snapshot",
            rollback_scope="turn",
            fork_supported=True,
            durable=True,
            shared_across_pods=True,
        )

    async def close(self, handle: RunHandle) -> None:
        # durable 状态进程外存活：进程退出（close_all）不销毁 checkpoint。
        return None

    async def attach(self, handle: RunHandle) -> RunHandle:
        if handle.run_id not in self._durable:
            raise ValueError(f"unknown run handle: {handle.run_id!r}")
        self.attach_calls.append(handle.run_id)
        return handle

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        return await self.attach(handle)

    def capabilities(self) -> RuntimeCapabilityMatrix:
        return RuntimeCapabilityMatrix(
            cancel=_capability(True),
            pause=_capability(False),
            resume=_capability(True),
            submit_interaction=_capability(False),
            attach=_capability(True),
            steer=_capability(False),
            inject=_capability(False),
            checkpoint=_capability(True),
            durable_restore=_capability(True),
        )


class _IdempotentExecutor:
    """按 idempotency key 去重的执行器：重放不重复执行。"""

    def __init__(self) -> None:
        self.executions: dict[str, str] = {}

    @property
    def execution_count(self) -> int:
        return len(self.executions)

    async def start(self, idempotency_key: str, run_id: str) -> None:
        self.executions.setdefault(idempotency_key, run_id)


class RecoveryHarness:
    """一个 (agent_instance, session) 的 fenced 写 + 崩溃窗口恢复 harness。"""

    def __init__(self, *, attachable: bool = False) -> None:
        self.kernel_store: InMemoryAgentKernelStore | None = None
        self.service = InMemorySessionService()
        self.event_store = SessionServiceEventStore(
            self.service, fence_validator=self._validate_fence
        )
        self.kernel_store = InMemoryAgentKernelStore(self.event_store)
        self.executor = _IdempotentExecutor()
        self.durable_handles: dict[str, RunHandle] = {}
        self.runtime_registry = RuntimeRegistry()
        runtime_self = self

        if attachable:

            def _factory(_context):
                return _AttachableAdapter(runtime_self.durable_handles)

            self.runtime_registry.register("fake", _factory)

        self.launch_context = RuntimeLaunchContext(
            runtime_type="fake", project_dir=Path(".")
        )
        self.capability_overrides: dict[str, bool] = {}
        self.continuation_ref: str | None = None
        self.idempotency_key = f"idem-{uuid4()}"
        self.run_id = f"run-{self.idempotency_key[:8]}"
        self.command_id = uuid4()
        self._session_ready = False

    async def ensure_session(self) -> None:
        if not self._session_ready:
            await self.service.create_session(
                agent_id="agent-1", user_id="user-1", session_id=SESSION
            )
            self._session_ready = True

    async def _validate_fence(self, envelope, guard) -> None:
        await self.kernel_store.validate_write_fence(envelope, guard)

    def capability_matrix(self) -> RuntimeCapabilityMatrix:
        overrides = self.capability_overrides

        def cap(field: str) -> RuntimeCapability:
            return _capability(overrides.get(field, False))

        return RuntimeCapabilityMatrix(
            cancel=cap("cancel"),
            pause=cap("pause"),
            resume=cap("resume"),
            submit_interaction=cap("submit_interaction"),
            attach=cap("attach"),
            steer=cap("steer"),
            inject=cap("inject"),
            checkpoint=cap("checkpoint"),
            durable_restore=cap("durable_restore"),
        )

    def runtime_executor(self) -> RuntimeExecutor:
        return RuntimeExecutor(
            self.runtime_registry, kernel_store=self.kernel_store
        )

    def write_context(self, activation: ActivationLease) -> ActivationWriteGuard:
        return ActivationWriteGuard(
            activation_id=activation.activation_id,
            fencing_token=activation.fencing_token,
        )

    def pipeline(self) -> CanonicalEventPipeline:
        store = RuntimeEventStore(self.event_store, session_id=SESSION)
        return CanonicalEventPipeline(store, session_id=SESSION)

    async def acquire(self, activation_id: str) -> ActivationLease:
        await self.ensure_session()
        return await self.kernel_store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id=AGENT, session_id=SESSION, activation_id=activation_id
            )
        )

    async def takeover(self) -> tuple[ActivationLease, ActivationLease]:
        old = await self.acquire("act-old")
        await self.kernel_store.release_activation(
            old.activation_id, expected_fence=old.fencing_token
        )
        new = await self.acquire("act-new")
        assert new.fencing_token > old.fencing_token
        return old, new

    async def submit_enqueue(self) -> None:
        command = AgentControlCommand(
            command_id=self.command_id,
            idempotency_key=self.idempotency_key,
            tenant_id="tenant-1",
            agent_instance_id=AGENT,
            session_id=SESSION,
            command_type="enqueue",
            payload={"content": "hello"},
            source=ControlSource(kind="studio", ref="test"),
            authorization_ref="permit-1",
            submitted_at="2026-08-17T00:00:00+00:00",
        )
        receipt = await self.kernel_store.accept_command(command, queue_limit=10)
        assert receipt.status == "accepted"

    async def drive(self, activation: ActivationLease, crash_point: str | None = None) -> None:
        """claim -> run 行 -> 执行 -> run.started -> 终态 -> ack，含崩溃注入。"""
        fence = activation.fencing_token
        guard = self.write_context(activation)
        message = await self.kernel_store.claim_next(AGENT, SESSION, fence)
        assert message is not None
        if crash_point == "after_claim":
            raise CrashPoint("after_claim")

        run = await self.kernel_store.load_run(self.run_id)
        if run is None:
            run = await self.kernel_store.save_run_transition(
                RunRecord(
                    run_id=self.run_id,
                    agent_instance_id=AGENT,
                    session_id=SESSION,
                    state=RunState.PENDING,
                ),
                expected_fence=fence,
            )
        if is_terminal_run(run.state):
            # 终态 first-wins：只补 ack，不再执行。
            await self.kernel_store.complete_claim(
                message.message_id, expected_fence=fence
            )
            return

        await self.executor.start(self.idempotency_key, self.run_id)
        if crash_point == "after_runtime_start":
            raise CrashPoint("after_runtime_start")

        running_update: dict[str, Any] = {
            "state": RunState.RUNNING,
            "handle": RunHandle(
                run_id=self.run_id,
                session_id=SESSION,
                runtime_type="fake",
            ).model_dump(mode="json"),
        }
        if self.continuation_ref:
            running_update["continuation_ref"] = self.continuation_ref
        running = run.model_copy(update=running_update)
        running.metadata["handle_digest"] = handle_digest(
            RunHandle.model_validate(running.metadata["handle"])
        )
        await self.kernel_store.save_run_transition(running, expected_fence=fence)
        pipeline = self.pipeline()
        await pipeline.emit(
            RunStarted(
                schema_version=2,
                event_id=f"{self.run_id}-started",
                seq=0,
                timestamp=1780000000.0,
                run_id=self.run_id,
                scope_id=f"run:{self.run_id}",
                status="running",
                source=_src(),
            ),
            write_context=guard,
        )
        if crash_point == "after_takeover":
            raise CrashPoint("after_takeover")

        completed = running.model_copy(update={"state": RunState.COMPLETED})
        await self.kernel_store.save_run_transition(completed, expected_fence=fence)
        await pipeline.emit(
            RunCompleted(
                schema_version=2,
                event_id=f"{self.run_id}-completed",
                seq=0,
                timestamp=1780000001.0,
                run_id=self.run_id,
                scope_id=f"run:{self.run_id}",
                status="completed",
                output_refs=(),
                source=_src(),
            ),
            write_context=guard,
        )
        if crash_point == "after_terminal_commit":
            raise CrashPoint("after_terminal_commit")

        await self.kernel_store.complete_claim(message.message_id, expected_fence=fence)

    def coordinator(self, *, executor: RuntimeExecutor | None = None) -> RecoveryCoordinator:
        return RecoveryCoordinator(
            self.kernel_store,
            self.event_store,
            self.capability_matrix,
            executor=executor,
            launch_context=self.launch_context if executor is not None else None,
        )

    async def terminal_event_count(self) -> int:
        store = RuntimeEventStore(self.event_store, session_id=SESSION)
        return sum(
            1
            for event in await store.list(SESSION)
            if event.run_id == self.run_id
            and event.event_type in TERMINAL_RUNTIME_EVENT_TYPES
        )

    async def crash_and_recover(self, crash_point: str) -> dict[str, int]:
        old = await self.acquire("act-old")
        await self.submit_enqueue()
        with pytest.raises(CrashPoint):
            await self.drive(old, crash_point)

        # takeover：旧 lease 释放，新 activation 以更高 fence 接管。
        await self.kernel_store.release_activation(
            old.activation_id, expected_fence=old.fencing_token
        )
        new = await self.acquire("act-new")
        assert new.fencing_token > old.fencing_token

        if crash_point == "after_takeover":
            # 旧 owner 的 stream 仍在吐 token：fence 必须拒绝。
            with pytest.raises(StaleFenceError):
                await self.pipeline().emit(
                    RunProgress(
                        schema_version=2,
                        event_id=f"{self.run_id}-late-token",
                        seq=0,
                        timestamp=1780000000.5,
                        run_id=self.run_id,
                        scope_id=f"run:{self.run_id}",
                        status="running",
                        progress=0.5,
                        source=_src(),
                    ),
                    write_context=self.write_context(old),
                )

        # 新 owner 先恢复 open run，再重放 inbox 剩余工作。
        await self.coordinator().recover(AGENT, new)
        await self.drive(new)
        return {
            "execution_count": self.executor.execution_count,
            "terminal_event_count": await self.terminal_event_count(),
        }


@pytest.fixture
async def harness() -> RecoveryHarness:
    return RecoveryHarness()


@pytest.fixture
async def recovery_harness() -> RecoveryHarness:
    return RecoveryHarness()


# --------------------------------------------------------------- Step 1 tests


@pytest.mark.asyncio
async def test_old_pipeline_cannot_append_after_takeover(harness: RecoveryHarness) -> None:
    old = await harness.acquire("act-old")
    old_pipeline = harness.pipeline()
    new_pipeline = harness.pipeline()

    await old_pipeline.emit(
        RunStarted(
            schema_version=2,
            event_id="evt-start",
            seq=0,
            timestamp=1780000000.0,
            run_id="run-fence",
            scope_id="run:run-fence",
            status="running",
            source=_src(),
        ),
        write_context=harness.write_context(old),
    )
    # takeover：旧 lease 释放，新 activation 以更高 fence 接管。
    await harness.kernel_store.release_activation(
        old.activation_id, expected_fence=old.fencing_token
    )
    new = await harness.acquire("act-new")
    assert new.fencing_token > old.fencing_token

    with pytest.raises(StaleFenceError):
        await old_pipeline.emit(
            RunProgress(
                schema_version=2,
                event_id="evt-delta",
                seq=0,
                timestamp=1780000000.5,
                run_id="run-fence",
                scope_id="run:run-fence",
                status="running",
                progress=0.5,
                source=_src(),
            ),
            write_context=harness.write_context(old),
        )
    await new_pipeline.emit(
        RunProgress(
            schema_version=2,
            event_id="evt-resumed",
            seq=0,
            timestamp=1780000000.6,
            run_id="run-fence",
            scope_id="run:run-fence",
            status="running",
            progress=0.6,
            source=_src(),
        ),
        write_context=harness.write_context(new),
    )

    # WriteContext 不进 RuntimeEvent payload，也不进公网 envelope projection。
    store = RuntimeEventStore(harness.event_store, session_id=SESSION)
    persisted = await store.list(SESSION)
    assert [event.event_id for event in persisted] == ["evt-start", "evt-resumed"]
    for event in persisted:
        assert "activation_id" not in event.model_dump()
        assert "fencing_token" not in event.model_dump()
    for envelope in await harness.event_store.read(SESSION, 0, 10):
        assert "activation_id" not in envelope.payload
        assert "fencing_token" not in envelope.payload


@pytest.mark.asyncio
async def test_handle_without_durable_attach_closes_interrupted(harness: RecoveryHarness) -> None:
    activation = await harness.acquire("act-old")
    await harness.submit_enqueue()
    with pytest.raises(CrashPoint):
        await harness.drive(activation, crash_point="after_takeover")

    await harness.kernel_store.release_activation(
        activation.activation_id, expected_fence=activation.fencing_token
    )
    new = await harness.acquire("act-new")

    report = await harness.coordinator().recover(AGENT, new)

    assert isinstance(report, RecoveryReport)
    assert report.outcome == "interrupted"
    assert report.reason == "runtime_not_durably_attachable"
    assert report.agent_instance_id == AGENT
    assert report.activation_id == new.activation_id
    assert report.run_id == harness.run_id
    run = await harness.kernel_store.load_run(harness.run_id)
    assert run is not None and run.state is RunState.INTERRUPTED
    assert await harness.terminal_event_count() == 1


# ------------------------------------------------------- 决策表其余分支


@pytest.mark.asyncio
async def test_terminal_run_recovery_is_no_op(harness: RecoveryHarness) -> None:
    activation = await harness.acquire("act-old")
    await harness.submit_enqueue()
    with pytest.raises(CrashPoint):
        await harness.drive(activation, crash_point="after_terminal_commit")

    await harness.kernel_store.release_activation(
        activation.activation_id, expected_fence=activation.fencing_token
    )
    new = await harness.acquire("act-new")

    report = await harness.coordinator().recover(AGENT, new)

    assert report.outcome == "no_op"
    assert await harness.terminal_event_count() == 1


@pytest.mark.asyncio
async def test_durable_handle_with_capabilities_attaches(harness: RecoveryHarness) -> None:
    attachable = RecoveryHarness(attachable=True)
    activation = await attachable.acquire("act-old")
    await attachable.submit_enqueue()
    with pytest.raises(CrashPoint):
        await attachable.drive(activation, crash_point="after_takeover")
    # 模拟 adapter start 已把 handle 放进 durable 状态。
    run = await attachable.kernel_store.load_run(attachable.run_id)
    attachable.durable_handles[attachable.run_id] = RunHandle.model_validate(
        run.metadata["handle"]
    )

    await attachable.kernel_store.release_activation(
        activation.activation_id, expected_fence=activation.fencing_token
    )
    new = await attachable.acquire("act-new")
    attachable.capability_overrides = {
        "attach": True,
        "durable_restore": True,
    }

    executor = attachable.runtime_executor()
    report = await attachable.coordinator(executor=executor).recover(AGENT, new)

    assert report.outcome == "attached"
    handle = executor.find_handle("fake", attachable.run_id, SESSION)
    assert handle is not None


@pytest.mark.asyncio
async def test_resume_capability_with_continuation_resumes(harness: RecoveryHarness) -> None:
    activation = await harness.acquire("act-old")
    await harness.submit_enqueue()
    harness.continuation_ref = "cont-1"
    with pytest.raises(CrashPoint):
        await harness.drive(activation, crash_point="after_takeover")

    await harness.kernel_store.release_activation(
        activation.activation_id, expected_fence=activation.fencing_token
    )
    new = await harness.acquire("act-new")
    harness.capability_overrides = {"resume": True}

    report = await harness.coordinator().recover(AGENT, new)

    assert report.outcome == "resumed"


# ------------------------------------------------------- Step 5 崩溃窗口


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_claim",
        "after_runtime_start",
        "after_terminal_commit",
        "after_takeover",
    ],
)
@pytest.mark.asyncio
async def test_recovery_crash_windows(crash_point: str, recovery_harness: RecoveryHarness) -> None:
    result = await recovery_harness.crash_and_recover(crash_point)
    assert result["execution_count"] == 1
    assert result["terminal_event_count"] <= 1
    if crash_point != "after_takeover":
        run = await recovery_harness.kernel_store.load_run(recovery_harness.run_id)
        assert run is not None and is_terminal_run(run.state)


# ------------------------------------------------- Step 6 跨进程恢复 E2E


@pytest.mark.asyncio
async def test_cross_executor_recovery_really_closes_old_executor() -> None:
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.sessions.in_memory import InMemorySessionService as Svc

    service = Svc()
    await service.create_session(agent_id="a", user_id="u", session_id=SESSION)
    kernel_store = InMemoryAgentKernelStore(SessionServiceEventStore(service))
    activation = await kernel_store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id=AGENT, session_id=SESSION, activation_id="act-old"
        )
    )
    durable_handles: dict[str, RunHandle] = {}
    registry_old = RuntimeRegistry()
    registry_old.register("fake", lambda _ctx: _AttachableAdapter(durable_handles))
    registry_new = RuntimeRegistry()
    registry_new.register("fake", lambda _ctx: _AttachableAdapter(durable_handles))
    context = RuntimeLaunchContext(runtime_type="fake", project_dir=Path("."))

    old_executor = RuntimeExecutor(registry_old, kernel_store=kernel_store)
    preparation = await old_executor.prepare_start(context)
    handle = await old_executor.start(
        context,
        StartRequest(
            input="go",
            user_id="u",
            session_id=SESSION,
            metadata={"run_id": "run-e2e"},
        ),
        preparation=preparation,
    )
    assert old_executor.is_attached(handle)
    assert durable_handles[handle.run_id] == handle

    # durable run 行携带 handle + digest（崩溃后唯一恢复线索）。
    pending = await kernel_store.save_run_transition(
        RunRecord(
            run_id=handle.run_id,
            agent_instance_id=AGENT,
            session_id=SESSION,
            state=RunState.PENDING,
        ),
        expected_fence=activation.fencing_token,
    )
    await kernel_store.save_run_transition(
        pending.model_copy(
            update={
                "state": RunState.RUNNING,
                "handle": handle.model_dump(mode="json"),
            }
        ),
        expected_fence=activation.fencing_token,
    )
    running = await kernel_store.load_run(handle.run_id)
    running.metadata["handle_digest"] = handle_digest(handle)

    # 真正关闭旧 executor：进程退出的等价物。
    await old_executor.close_all()
    assert not old_executor.is_attached(handle)

    # 新 executor：不复用 _runs，只能通过 durable 行 + attach 回来。
    new_executor = RuntimeExecutor(registry_new, kernel_store=kernel_store)
    assert new_executor.find_handle("fake", handle.run_id, SESSION) is None
    restored = await new_executor.attach_record(running, context)
    assert restored == handle
    assert new_executor.is_attached(restored)
    assert not old_executor.is_attached(restored)

    # digest 被篡改的 durable 行必须被拒绝。
    tampered = running.model_copy(update={})
    tampered.metadata["handle_digest"] = "0" * 64
    third = RuntimeExecutor(registry_new, kernel_store=kernel_store)
    with pytest.raises(ValueError, match="digest"):
        await third.attach_record(tampered, context)
