# -*- coding: utf-8 -*-
"""per-session FIFO worker（Kernel Task 6 Step 6）。

- 持有 activation lease（fencing token 通过 Store 的 CAS 校验）才能 claim。
- 按 per-session accepted_seq 保序；active Run 存在时普通 enqueue 保持排队，
  只执行允许作用于该 Run 的控制命令（interrupt/pause/steer/...）。
- 异常分类：retryable kernel error（消息保持 claimed）、typed runtime
  rejection（discarded + control.command_rejected）、terminal failure
  （不 ack，消息保持 claimed 等待 takeover reclaim）。

Task 6：Activation 通过 :class:`ActiveExecution` 拥有 Adapter/RunHandle/
InteractionProvider——控制命令与 Interaction 回包永远作用于同一 live
execution（同一 client 实例）；control lookup 永远按 durable run id。
``submit_interaction`` 不再是静态 ``adapter.submit`` 映射：Worker 载入权威
``InteractionRecord``，调用其绑定 provider 送达回包，provider 接受后才写
``InteractionResolved``，同 fence 恢复 stream 消费。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ksadk.interaction.contracts import (
    InteractionSubmission,
)
from ksadk.interaction.provider import (
    RUNTIME_INTERACTION_UNAVAILABLE,
    InteractionResolveContext,
)
from ksadk.interaction.submission import normalized_submission
from ksadk.kernel.contracts import (
    ActivationLease,
    ActivationWriteGuard,
    AgentControlCommand,
    SubmitInteractionPayload,
)
from ksadk.kernel.contracts import (
    InjectPayload as ContractInjectPayload,
)
from ksadk.kernel.contracts import (
    SteerPayload as ContractSteerPayload,
)
from ksadk.kernel.errors import (
    AgentKernelError,
    InvalidCommandError,
    UnsupportedControlError,
)
from ksadk.kernel.execution_grants import (
    execution_grant_run_id,
)
from ksadk.kernel.mapping import COMMAND_HANDLERS, RESUME_TARGET_KINDS
from ksadk.kernel.state import RunState
from ksadk.kernel.store import (
    RunRecord,
)
from ksadk.runtime.adapter import (
    CancelResult,
    PauseResult,
)
from ksadk.runtime.adapter import (
    ResumePayload as AdapterResumePayload,
)
from ksadk.runtime.adapter import (
    ResumeTarget as AdapterResumeTarget,
)

if TYPE_CHECKING:
    from ksadk.kernel.worker import ActiveExecution


logger = logging.getLogger(__name__)


class WorkerControlMixin:
    """Control admission and durable interaction responses for active runs."""

    async def _interaction_rejection_run_id(self, command: AgentControlCommand) -> str | None:
        """Attribute an answer rejection only through the active, scoped ledger.

        A submitted RunId/InteractionId is not proof of ownership. Re-read both
        persisted records before making the diagnostic visible to a run stream.
        """
        if command.command_type != "submit_interaction":
            return None
        active = await self._store.find_active_run(command.agent_instance_id, command.session_id)
        target = command.payload.get("run_id")
        if (
            active is None
            or (target is not None and target != active.run_id)
            or active.agent_instance_id != command.agent_instance_id
            or active.session_id != command.session_id
            or active.metadata.get("tenant_id") != command.tenant_id
        ):
            return None
        record = await self._store.get(
            command.payload.get("interaction_id"), tenant_id=command.tenant_id,
            agent_instance_id=command.agent_instance_id, session_id=command.session_id,
            run_id=active.run_id,
        )
        if record is None or (
            record.tenant_id, record.agent_instance_id, record.session_id, record.run_id
        ) != (command.tenant_id, command.agent_instance_id, command.session_id, active.run_id):
            return None
        return record.run_id

    async def _control_active_run(
        self, command: AgentControlCommand, activation: ActivationLease
    ) -> str | None:
        fence = activation.fencing_token
        active = await self._store.find_active_run(command.agent_instance_id, command.session_id)
        target_run_id = str(command.payload.get("run_id") or "").strip()
        if active is None or (target_run_id and active.run_id != target_run_id):
            # A Studio cancel can race with an admitted enqueue before the
            # worker has materialized its durable RunRecord.  Consume the
            # matching pending enqueue under the same fence so cancellation
            # remains durable instead of falling back to a process-local
            # operation handle.
            if command.command_type == "interrupt" and command.payload.get("scope_id") is None:
                target_run_id = str(command.payload.get("run_id") or "").strip()
                if target_run_id:
                    for pending in await self._store.list_pending(
                        command.agent_instance_id,
                        command.session_id,
                        fencing_token=fence,
                    ):
                        candidate = pending.command
                        if candidate is None or candidate.command_type != "enqueue":
                            continue
                        try:
                            candidate_run_id = execution_grant_run_id(candidate)
                        except Exception:
                            continue
                        if candidate_run_id != target_run_id:
                            continue
                        claimed = await self._store.claim_message(pending.message_id, fence)
                        await self._store.discard_claim(
                            claimed.message_id,
                            expected_fence=fence,
                        )
                        return target_run_id
            raise UnsupportedControlError(
                "runtime_no_active_run",
                details={"command_type": command.command_type},
            )
        execution = self._executions.get(active.run_id)
        if self._mcp_authorization is not None:
            oauth_waits = await self._mcp_authorization.pending(active.run_id)
            if oauth_waits and (command.command_type == "resume" or (
                    command.command_type == "submit_interaction" and any(
                        command.payload.get("interaction_id") in {r["interaction_id"], r["wait_id"]} for r in oauth_waits))):
                raise InvalidCommandError("OAuth waits require an internal authorization completion")
            if oauth_waits and command.command_type == "interrupt" and not command.payload.get("scope_id"):
                await self._mcp_authorization.cancel_run(self, active, ActivationWriteGuard(
                    activation_id=activation.activation_id, fencing_token=fence))
        a2a_origin = command.payload.get("a2a_origin_command_id")
        if a2a_origin is not None and (
            not isinstance(a2a_origin, str) or not a2a_origin
            or active.metadata.get("command_id") != a2a_origin
            or (command.payload.get("run_id") is not None and command.payload["run_id"] != active.run_id)
        ):
            raise InvalidCommandError("A2A control does not belong to the original admitted run")
        if execution is None:
            raise UnsupportedControlError(
                "runtime_not_attached",
                details={"run_id": active.run_id},
            )
        # Task 6：控制命令作用在 activation 拥有的同一 adapter/handle 上，
        # 绝不新建 adapter（那会把命令打到没有 live 状态的实例上）。
        adapter = execution.adapter
        handle = execution.handle
        verb = COMMAND_HANDLERS[command.command_type]

        if verb == "cancel" and command.payload.get("scope_id") is not None:
            scope_id = command.payload["scope_id"]
            capability = adapter.capabilities().scoped_cancel
            if capability is None or not capability.supported or self._session_events is None:
                raise UnsupportedControlError("runtime_scoped_cancel_unsupported")
            from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority

            authority = RunEventAuthority(
                self._session_events, session_id=active.session_id, run_id=active.run_id,
                guard=ActivationWriteGuard(
                    activation_id=activation.activation_id, fencing_token=fence
                ),
                interaction_ledger=self._store, tenant_id=command.tenant_id,
                agent_instance_id=command.agent_instance_id,
                managed_a2a_bootstraps=execution.managed_a2a_bootstraps,
            )
            try:
                with bind_run_event_authority(authority):
                    await authority.cancel_a2a_scope(scope_id)
            except (KeyError, ValueError) as exc:
                raise UnsupportedControlError("runtime_child_scope_not_found") from exc
            # Acceptance is a durable child cancel intent. Its descriptor owns
            # confirmation; the root stream/adapter/run remains live.
            return active.run_id

        if verb == "cancel":
            if await self._cancel_suspended_langgraph(execution, active, activation):
                return active.run_id
            result = await adapter.cancel(handle)
            if result == CancelResult.INTERRUPTED_ACTIVE_TURN:
                for interaction_id, waiter in tuple(execution.managed_interaction_waiters.items()):
                    record = await self._store.get(
                        interaction_id, tenant_id=command.tenant_id,
                        agent_instance_id=command.agent_instance_id,
                        session_id=command.session_id, run_id=active.run_id,
                    )
                    if record is not None and record.status == "pending":
                        await self._store.cancel(
                            interaction_id, record.revision,
                            guard=ActivationWriteGuard(
                                activation_id=activation.activation_id, fencing_token=fence
                            ),
                        )
                    waiter.close()
                    execution.managed_interaction_waiters.pop(interaction_id, None)
                if execution.stream_task is not None and not execution.stream_task.done():
                    execution.stream_task.cancel()
                    await asyncio.gather(execution.stream_task, return_exceptions=True)
                if self._session_events is not None:
                    from ksadk.events.canonical import RunCanceled, SourceRef
                    from ksadk.events.canonical_store import RuntimeEventStore

                    events = RuntimeEventStore(self._session_events, session_id=active.session_id)
                    recorded = await events.list(active.session_id, run_id=active.run_id)
                    started = next((event for event in recorded if event.event_type == "run.started"), None)
                    # A cancelled stream can already have persisted its terminal
                    # fact. Preserve that fact and its original root scope.
                    if not any(event.event_type == "run.canceled" for event in recorded):
                        await events.append(
                            RunCanceled(
                                schema_version=2,
                                event_id=f"{active.run_id}:kernel-canceled",
                                seq=0,
                                timestamp=datetime.now(timezone.utc).timestamp(),
                                run_id=active.run_id,
                                scope_id=(started.scope_id if started is not None else f"run:{active.run_id}"),
                                source=SourceRef(
                                    framework=(
                                        handle.runtime_type
                                if handle.runtime_type in {
                                    "adk", "langgraph", "codex", "a2a", "ksadk"
                                }
                                        else "ksadk"
                                    )
                                ),
                                status="canceled",
                                reason=str(command.payload.get("reason") or "user_cancel"),
                            ),
                            guard=ActivationWriteGuard(
                                activation_id=activation.activation_id, fencing_token=fence
                            ),
                        )
                await self._transition_run(active, RunState.CANCELLED, fence)
                self._executions.pop(active.run_id, None)
                await adapter.close(handle)
        elif verb == "pause":
            result = await adapter.pause(handle)
            if result == PauseResult.PAUSED_ACTIVE_TURN:
                await self._transition_run(active, RunState.PAUSED, fence)
        elif verb == "resume":
            target_dict = dict(command.payload.get("target") or {})
            target = AdapterResumeTarget(
                kind=RESUME_TARGET_KINDS[target_dict["kind"]],
                id=str(target_dict["id"]),
            )
            resumed = await adapter.resume(
                handle,
                target,
                AdapterResumePayload(
                    kind="free_text", session_context=command.payload.get("session_context")
                ),
            )
            execution = self._replace_handle(execution, resumed)
            self._start_stream(
                execution,
                active,
                ActivationWriteGuard(
                    activation_id=activation.activation_id,
                    fencing_token=fence,
                ),
            )
        elif verb == "submit_interaction":
            await self._submit_interaction(command, activation, active, execution)
        elif verb == "steer":
            await adapter.steer(handle, ContractSteerPayload.model_validate(dict(command.payload)))
        elif verb == "inject":
            await adapter.inject(
                handle, ContractInjectPayload.model_validate(dict(command.payload))
            )
        else:  # pragma: no cover - mapping 冻结
            raise UnsupportedControlError(f"unknown handler {verb!r}")
        return active.run_id

    # ------------------------------------------------- Task 6: interaction 回包

    async def _submit_interaction(
        self,
        command: AgentControlCommand,
        activation: ActivationLease,
        active: RunRecord,
        execution: ActiveExecution,
    ) -> None:
        """权威 record -> 绑定 provider -> provider 接受后才写 resolved。

        顺序是合同：provider 拒绝（含 unavailable）时 Interaction 保持
        pending，绝不提前标 resolved；ledger ``resolve`` 本身做 revision CAS
        first-wins。
        """

        fence = activation.fencing_token
        payload = SubmitInteractionPayload.model_validate(dict(command.payload))
        record = await self._store.get(  # type: ignore[attr-defined]
            payload.interaction_id,
            tenant_id=command.tenant_id,
            agent_instance_id=command.agent_instance_id,
            session_id=command.session_id,
            run_id=active.run_id,
        )
        if record is None:
            raise InvalidCommandError(
                f"unknown interaction_id {payload.interaction_id!r}",
                details={"interaction_id": payload.interaction_id},
            )
        if command.payload.get("a2a_interaction_kind") is not None and command.payload["a2a_interaction_kind"] != record.kind:
            raise InvalidCommandError("A2A interaction kind differs from the pending Kernel record")
        if record.run_id != active.run_id:
            raise InvalidCommandError(
                "interaction does not belong to the active run",
                details={
                    "interaction_id": record.interaction_id,
                    "interaction_run_id": record.run_id,
                    "active_run_id": active.run_id,
                },
            )
        # ``ActiveExecution`` owns the adapter, live handle *and* provider for
        # this activation.  The durable record tells us what was requested,
        # but it must not redirect a response into another framework provider:
        # e.g. calling LangGraph checkpoint resume with a Codex live handle
        # would acknowledge a response that can never reach the original run.
        if record.provider_id == "managed-a2a":
            waiter = execution.managed_interaction_waiters.get(record.interaction_id)
            if waiter is None or not waiter.accepts(record):
                raise AgentKernelError(
                    RUNTIME_INTERACTION_UNAVAILABLE,
                    "managed A2A invocation is not waiting in this execution",
                    retryable=False,
                )
            allowed = {"approve", "reject"} if record.kind == "approval" else {"submit", "reject"}
            if payload.action not in allowed:
                raise InvalidCommandError("invalid managed A2A interaction action")
            submission = InteractionSubmission(
                interaction_id=record.interaction_id,
                expected_revision=(payload.expected_revision
                                   if payload.expected_revision is not None
                                   else record.revision),
                action=payload.action,
                response=payload.response,
                idempotency_key=payload.idempotency_key or command.idempotency_key,
            )
            delivery = normalized_submission(record, submission)
            receipt = await self._store.resolve(
                submission, guard=ActivationWriteGuard(
                    activation_id=activation.activation_id, fencing_token=fence
                ),
            )
            if active.state is not RunState.RUNNING:
                await self._transition_run(active, RunState.RUNNING, fence)
            waiter.deliver(receipt, delivery.response)
            execution.managed_interaction_waiters.pop(record.interaction_id, None)
            return
        provider = execution.interaction_provider
        if provider.provider_id != record.provider_id or provider.mode == "unavailable":
            raise AgentKernelError(
                RUNTIME_INTERACTION_UNAVAILABLE,
                f"interaction provider {record.provider_id!r} cannot deliver "
                "the response through the active execution's native framework "
                "identity",
                retryable=False,
                details={
                    "provider_id": record.provider_id,
                    "active_provider_id": provider.provider_id,
                    "mode": provider.mode,
                    "interaction_id": record.interaction_id,
                },
            )
        submission = InteractionSubmission(
            interaction_id=record.interaction_id,
            expected_revision=int(
                payload.expected_revision
                if payload.expected_revision is not None
                else record.revision
            ),
            action=payload.action or "submit",  # type: ignore[arg-type]
            response=payload.response,
            idempotency_key=payload.idempotency_key or command.idempotency_key,
        )
        delivery = normalized_submission(record, submission)
        context = InteractionResolveContext(
            adapter=execution.adapter,
            handle=execution.handle,
            session_context=command.payload.get("session_context"),
            activation_id=activation.activation_id,
            fencing_token=fence,
        )
        guard = ActivationWriteGuard(activation_id=activation.activation_id, fencing_token=fence)
        deferred = bool(record.provider_id == "langgraph" and (record.native_target or {}).get("task_id")
                        and getattr(execution.adapter, "confirms_native_submissions", False))
        if deferred:
            task = execution.stream_task
            if task is not None and not task.done():
                # A request fact is durable before its stream reaches EOF.
                # Never register a resume while the prior stream's finally
                # block can still discard the same Adapter invocation.
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except TimeoutError as error:
                    raise AgentKernelError(
                        RUNTIME_INTERACTION_UNAVAILABLE,
                        "Native wait is still being finalized", retryable=True,
                    ) from error
            if submission.action == "cancel":
                if record.status != "pending" or record.revision != submission.expected_revision:
                    raise InvalidCommandError("Interaction is no longer cancellable at this revision")
                if not await self._cancel_suspended_langgraph(execution, active, activation):
                    raise InvalidCommandError("Native wait cannot be cancelled in its current state")
                return
            remember = getattr(execution.adapter, "remember_native_interactions", None)
            if callable(remember):
                pending = await self._store.list_pending_interactions(command.tenant_id, command.session_id)
                remember(execution.handle, [item for item in pending
                         if item.run_id == active.run_id
                         and item.agent_instance_id == command.agent_instance_id])
            reserve = getattr(self._store, "reserve_submission", None)
            if not callable(reserve):
                raise InvalidCommandError("This store does not support native submission reservations")
            reservation = await reserve(submission, guard=guard, command_id=str(command.command_id))
            if not reservation.acquired:
                return
        try:
            resumed = await provider.resolve(context, record, delivery)
        except Exception:
            # resume() only registers an invocation. No graph has run yet;
            # releasing this reservation is therefore safe and correctable.
            if deferred:
                await self._store.release_submission(submission, guard=guard)
            raise
        execution = self._replace_handle(execution, resumed)
        if not deferred:
            await self._store.resolve(submission, guard=guard)  # type: ignore[attr-defined]
        # A durable response returns a waiting run to execution.  The old live
        # stream usually remains open (Codex); checkpoint providers normally
        # returned a fresh handle and need a new background stream.  RUNNING is
        # already the active-execution state (RUNNING -> RUNNING is not a legal
        # transition), so only WAITING/PAUSED runs move back to RUNNING.
        resumed_run = active
        if active.state != RunState.RUNNING:
            resumed_run = await self._transition_run(active, RunState.RUNNING, fence)
        task = execution.stream_task
        if task is None or task.done():
            self._start_stream(
                execution,
                resumed_run,
                ActivationWriteGuard(
                    activation_id=activation.activation_id,
                    fencing_token=fence,
                ),
            )

    async def _cancel_suspended_langgraph(
        self, execution: ActiveExecution, active: RunRecord, activation: ActivationLease
    ) -> bool:
        """Retire a native checkpoint wait without scheduling another graph turn.

        Native interrupt streams have already ended. The generic runner's
        pending-cancel result only affects a future turn, so the Kernel must
        settle its own durable WAITING run and interaction ledger here.
        """
        if (
            active.state is not RunState.WAITING
            or execution.handle.runtime_type != "langgraph"
            or self._session_events is None
            or (execution.stream_task is not None and not execution.stream_task.done())
        ):
            return False
        from ksadk.events.canonical import RunCanceled, SourceRef
        from ksadk.events.canonical_store import RuntimeEventStore

        events = RuntimeEventStore(self._session_events, session_id=active.session_id)
        event_id = f"{active.run_id}:kernel-checkpoint-canceled"
        existing = await events.event_by_id(active.session_id, event_id)
        records = [record for record in await self._store.list_pending_interactions(
            str(active.metadata.get("tenant_id") or ""), active.session_id
        ) if record.run_id == active.run_id and record.agent_instance_id == active.agent_instance_id]
        oauth_targets = (await self._mcp_authorization.cancellation_targets(active.run_id)
                         if self._mcp_authorization is not None else [])
        targets = [record.native_target for record in records] + oauth_targets
        if existing is None and (not targets or any(record.provider_id != "langgraph" for record in records)
                or any(not isinstance(target, dict) or any(
                    not target.get(key) or target[key] != execution.handle.native_ref.get(key)
                    for key in ("thread_id", "checkpoint_id")) for target in targets)):
            return False
        guard = ActivationWriteGuard(
            activation_id=activation.activation_id, fencing_token=activation.fencing_token
        )
        # Write the stable terminal identity first so reclaiming this command
        # can finish ledger cleanup even if the previous worker stopped midway.
        if existing is None:
            started = next((event for event in await events.list(
                active.session_id, run_id=active.run_id, event_types=["run.started"]
            ) if event.parent_scope_id is None), None)
            if started is None:
                raise RuntimeError("Native checkpoint cancellation requires the original run scope")
            await events.append(RunCanceled(
                schema_version=2, event_id=event_id, seq=0,
                timestamp=datetime.now(timezone.utc).timestamp(), run_id=active.run_id,
                scope_id=started.scope_id,
                source=SourceRef(framework="ksadk", native_run_id=execution.runtime_run_id),
                status="canceled", reason="native_checkpoint_wait_cancelled",
            ), guard=guard)
        retire_wait = getattr(execution.adapter, "retire_cancelled_wait", None)
        if callable(retire_wait):
            await retire_wait(execution.handle)
        for record in records:
            await self._store.cancel(record.interaction_id, record.revision, guard=guard)
        await self._transition_run(active, RunState.CANCELLED, activation.fencing_token)
        self._executions.pop(active.run_id, None)
        try:
            await execution.adapter.close(execution.handle)
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to close canceled checkpoint transport for durable run %s",
                active.run_id,
            )
        return True
