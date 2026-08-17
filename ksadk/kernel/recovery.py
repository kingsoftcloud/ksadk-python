# -*- coding: utf-8 -*-
"""冷恢复决策表：RecoveryCoordinator（Phase 1 Task 7）。

接管一个 agent_instance 的 open run 时按固定决策表收口：

- run 已终态（或不存在 open run）→ ``no_op``；
- ``attach`` + ``durable_restore`` 能力可用且 durable handle digest 有效 →
  ``attach``（跨进程接回 live handle）；
- ``resume`` 能力可用且存在 continuation → ``resume``（从最后 continuation 续跑）；
- 否则 → 确定性 ``interrupted``（唯一 ``run.interrupted`` + open item close），
  reason 固定为 ``runtime_not_durably_attachable``。

每个决定都追加一条 fenced ``control.recovery_decided`` 审计事实；
``RecoveryReport`` 只用于审计与测试，不进入公网 projection。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ksadk.events.canonical import RuntimeEvent
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.cold_recovery import scan_open_runs, settle_finding
from ksadk.events.pipeline import CanonicalEventPipeline
from ksadk.events.session_event import SessionEventStore
from ksadk.kernel.contracts import (
    ActivationLease,
    ActivationWriteGuard,
    RuntimeCapabilityMatrix,
    WriteContext,
)
from ksadk.kernel.state import RunState, is_terminal_run
from ksadk.kernel.store import AgentKernelStore, RunRecord, control_event

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ksadk.runtime.executor import RuntimeExecutor
    from ksadk.runtime.launch import RuntimeLaunchContext

RecoveryOutcome = Literal["no_op", "attached", "resumed", "interrupted", "failed"]

CapabilityProvider = Callable[[], RuntimeCapabilityMatrix]


@dataclass
class RecoveryReport:
    """一次 recover 决策的审计结果（不进入公网 projection）。"""

    agent_instance_id: str
    activation_id: str
    run_id: str | None = None
    outcome: RecoveryOutcome = "no_op"
    reason: str | None = None
    last_seq: int | None = None
    written_events: list[RuntimeEvent] = field(default_factory=list)


def _durable_handle_digest(handle_dump: dict) -> str | None:
    """durable handle 行携带的 digest；缺失/形状不符返回 None。"""

    try:
        from ksadk.runtime.adapter import RunHandle
        from ksadk.runtime.executor import handle_digest

        return handle_digest(RunHandle.model_validate(handle_dump))
    except Exception:
        return None


class RecoveryCoordinator:
    """对 open run 做确定性收口或接管的协调器。"""

    def __init__(
        self,
        store: AgentKernelStore,
        session_events: SessionEventStore,
        capabilities: CapabilityProvider,
        *,
        executor: "RuntimeExecutor | None" = None,
        launch_context: "RuntimeLaunchContext | None" = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._session_events = session_events
        self._capabilities = capabilities
        self._executor = executor
        self._launch_context = launch_context
        self._clock = clock

    async def recover(
        self,
        agent_instance_id: str,
        activation: ActivationLease,
        *,
        run_id: str | None = None,
    ) -> RecoveryReport:
        fence = activation.fencing_token
        guard: WriteContext = ActivationWriteGuard(
            activation_id=activation.activation_id, fencing_token=fence
        )
        run = await self._load_run(agent_instance_id, run_id)
        if run is None or is_terminal_run(run.state):
            return await self._decide(
                agent_instance_id,
                activation,
                run,
                outcome="no_op",
                reason=(
                    "run_already_terminal"
                    if run is not None
                    else "no_open_run_for_agent_instance"
                ),
                guard=guard,
            )

        capabilities = self._capabilities()
        handle_dump = run.metadata.get("handle")
        handle_digest_valid = (
            isinstance(handle_dump, dict)
            and isinstance(run.metadata.get("handle_digest"), str)
            and _durable_handle_digest(handle_dump) == run.metadata.get("handle_digest")
        )
        if (
            capabilities.attach.supported
            and capabilities.durable_restore.supported
            and handle_digest_valid
            and self._executor is not None
            and self._launch_context is not None
        ):
            try:
                await self._executor.attach_record(run, self._launch_context)
            except Exception as error:
                return await self._decide(
                    agent_instance_id,
                    activation,
                    run,
                    outcome="failed",
                    reason=f"attach_failed:{type(error).__name__}",
                    guard=guard,
                )
            return await self._decide(
                agent_instance_id,
                activation,
                run,
                outcome="attached",
                reason="durable_handle_attached",
                guard=guard,
            )

        if capabilities.resume.supported and run.metadata.get("continuation_ref"):
            return await self._decide(
                agent_instance_id,
                activation,
                run,
                outcome="resumed",
                reason="continuation_resume_delegated",
                guard=guard,
            )

        return await self._interrupt_deterministically(
            agent_instance_id, activation, run, guard=guard
        )

    # ------------------------------------------------------------- internals

    async def _load_run(
        self, agent_instance_id: str, run_id: str | None
    ) -> RunRecord | None:
        if run_id is not None:
            return await self._store.load_run(run_id)
        finder = getattr(self._store, "find_active_run", None)
        if finder is not None:
            return await finder(agent_instance_id)
        return None

    async def _interrupt_deterministically(
        self,
        agent_instance_id: str,
        activation: ActivationLease,
        run: RunRecord,
        *,
        guard: WriteContext,
    ) -> RecoveryReport:
        fence = activation.fencing_token
        runtime_store = RuntimeEventStore(self._session_events, session_id=run.session_id)
        findings = await scan_open_runs(runtime_store, run.session_id)
        finding = next((item for item in findings if item.run_id == run.run_id), None)
        written: list[RuntimeEvent] = []
        last_seq: int | None = None
        if finding is not None:
            events = settle_finding(
                finding,
                run.session_id,
                allow_resume=False,
                timestamp=self._clock(),
                reason="runtime_not_durably_attachable",
            )
            pipeline = CanonicalEventPipeline(runtime_store, session_id=run.session_id)
            for event in events:
                persisted = await pipeline.emit(event, write_context=guard)
                written.append(persisted)
                last_seq = persisted.seq
        await self._store.save_run_transition(
            run.model_copy(update={"state": RunState.INTERRUPTED}),
            expected_fence=fence,
        )
        return await self._decide(
            agent_instance_id,
            activation,
            run,
            outcome="interrupted",
            reason="runtime_not_durably_attachable",
            guard=guard,
            written=written,
            last_seq=last_seq,
        )

    async def _decide(
        self,
        agent_instance_id: str,
        activation: ActivationLease,
        run: RunRecord | None,
        *,
        outcome: RecoveryOutcome,
        reason: str,
        guard: WriteContext,
        written: list[RuntimeEvent] | None = None,
        last_seq: int | None = None,
    ) -> RecoveryReport:
        if run is not None:
            await self._store.append_event(
                control_event(
                    session_id=run.session_id,
                    event_type="control.recovery_decided",
                    payload={
                        "agent_instance_id": agent_instance_id,
                        "activation_id": activation.activation_id,
                        "fencing_token": activation.fencing_token,
                        "run_id": run.run_id,
                        "outcome": outcome,
                        "reason": reason,
                    },
                    run_id=run.run_id,
                ),
                expected_fence=activation.fencing_token,
                agent_instance_id=agent_instance_id,
            )
        return RecoveryReport(
            agent_instance_id=agent_instance_id,
            activation_id=activation.activation_id,
            run_id=run.run_id if run is not None else None,
            outcome=outcome,
            reason=reason,
            last_seq=last_seq,
            written_events=list(written or []),
        )


def durable_handle_digest(handle_dump: dict) -> str | None:
    """Public shim kept for callers that only need digest validation."""
    return _durable_handle_digest(handle_dump)


__all__ = [
    "RecoveryCoordinator",
    "RecoveryReport",
    "RecoveryOutcome",
    "durable_handle_digest",
]
