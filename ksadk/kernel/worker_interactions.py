"""Framework interaction persistence and managed delivery owned by the worker."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ksadk.interaction.ledger import ALREADY_RESOLVED, REVISION_MISMATCH
from ksadk.kernel.approval_presentation import approval_presentation
from ksadk.kernel.contracts import (
    ActivationWriteGuard,
)
from ksadk.kernel.errors import (
    InvalidCommandError,
)
from ksadk.kernel.native_confirmation import NativeConfirmationMixin
from ksadk.kernel.state import RunState
from ksadk.kernel.store import (
    RunRecord,
)

if TYPE_CHECKING:
    from ksadk.kernel.worker import ActiveExecution


logger = logging.getLogger(__name__)


class WorkerInteractionMixin(NativeConfirmationMixin):
    async def _publish_wait_ready(self, execution, run, guard):
        """Wire flush marker, not another interaction or terminal Run fact."""
        if self._session_events is None:
            return
        from ksadk.kernel.store import control_event

        ids = [
            item.interaction_id
            for item in await self._store.list_pending_interactions(
                str(run.metadata.get("tenant_id") or ""), run.session_id
            )
            if item.run_id == run.run_id
            and item.agent_instance_id == run.agent_instance_id
            and item.status == "pending"
        ]
        if self._mcp_authorization is not None:
            ids.extend(row["wait_id"] for row in await self._mcp_authorization.pending(run.run_id))
        if ids:
            await self._session_events.append(
                control_event(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    event_type="control.interaction_wait_ready",
                    payload={"interaction_ids": ids},
                ),
                guard=guard,
            )

    async def _record_interaction_request(
        self,
        execution: ActiveExecution,
        run: RunRecord,
        event: object,
        guard: ActivationWriteGuard,
    ) -> RunRecord:
        """Persist one framework interaction as the durable ledger authority."""

        from ksadk.events.canonical import ApprovalRequest, InteractionRequested
        from ksadk.interaction.contracts import InteractionPresentation, InteractionRecord

        assert isinstance(event, InteractionRequested)
        from ksadk.mcp_runtime.oauth.langgraph import MARKER
        if MARKER in getattr(event.request, "schema_", {}):
            if self._mcp_authorization is None:
                raise InvalidCommandError("MCP OAuth Kernel extension is unavailable")
            return await self._mcp_authorization.record(self, execution, run, event, guard)
        presentation = None
        if isinstance(event.request, ApprovalRequest):
            request_schema = {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["approve", "reject"],
                    }
                },
                "required": ["decision"],
            }
            native_target = {"call_id": event.request.call_id or event.interaction_id}
            presentation = approval_presentation(event.request)
        else:
            request_schema = dict(event.request.schema_)
            if event.request.prompt:
                presentation = {"title": event.request.prompt}
            # Codex maps the native JSON-RPC request id to a stable canonical
            # interaction id for replay.  The live client, however, indexes its
            # pending callback by the original request id.  Preserve that id as
            # the provider target or SubmitInteraction can find the durable
            # record but cannot wake the blocked Codex callback.
            native_call_id = (
                event.source.native_event_id or event.source.native_item_id or event.interaction_id
            )
            native_target = {"call_id": native_call_id}
        for key in ("checkpoint_id", "thread_id", "checkpoint_ns"):
            value = execution.handle.native_ref.get(key)
            if value is not None:
                native_target[key] = str(value)
        from ksadk.interaction.native_events import NativeInteractionRequested

        if isinstance(event, NativeInteractionRequested):
            native_target = dict(event._native_target)
            if event._presentation is not None:
                if isinstance(presentation, InteractionPresentation):
                    presentation = presentation.model_dump(mode="json", exclude_none=True)
                presentation = {**(presentation or {}), **event._presentation}
        if isinstance(presentation, dict):
            presentation.setdefault("title", "补充信息")
        if native_target.get("legacy_a2ui_binding"):
            # Select the legacy component answer envelope without changing validation.
            request_schema = {**request_schema,
                "x-ksadk-a2ui-surface-id": native_target["a2ui_surface_id"]}
        provider_id = execution.interaction_provider.provider_id or execution.handle.runtime_type
        managed_request = None
        if getattr(event, "_invocation", None) is not None:
            from ksadk.a2a.runtime_approval import approval_target, bound_approval_request

            managed_request = bound_approval_request(
                event, run_id=run.run_id, session_id=run.session_id
            )
        if managed_request is not None:
            provider_id = "managed-a2a"
            native_target = approval_target(managed_request)
            if event._waiter is not None:
                execution.managed_interaction_waiters[event.interaction_id] = event._waiter
        if getattr(event, "_managed_input_waiter", None) is not None:
            from ksadk.a2a.runtime_input import bound_input_waiter

            waiter = bound_input_waiter(event, run_id=run.run_id, session_id=run.session_id)
            if waiter is not None:
                provider_id, native_target = "managed-a2a", waiter.target
                execution.managed_interaction_waiters[event.interaction_id] = waiter
        record = InteractionRecord(
            interaction_id=event.interaction_id,
            tenant_id=str(run.metadata.get("tenant_id") or ""),
            agent_instance_id=run.agent_instance_id,
            session_id=run.session_id,
            run_id=run.run_id,
            kind=event.interaction_kind,
            request_schema=request_schema,
            created_at=datetime.fromtimestamp(event.timestamp, timezone.utc).isoformat(),
            expires_at=(
                datetime.fromtimestamp(
                    managed_request.deadline_unix_seconds, timezone.utc
                ).isoformat()
                if managed_request is not None
                else None
            ),
            presentation=presentation,
            provider_id=provider_id,
            native_target=native_target,
            continuation_metadata={"runtime_run_id": execution.runtime_run_id},
        )
        persisted = await self._store.request(record, guard=guard)  # type: ignore[attr-defined]
        delivered = False
        if provider_id == "managed-a2a" and persisted.status not in {"pending", "resolving"}:
            waiter = execution.managed_interaction_waiters.get(record.interaction_id)
            if waiter is None or not waiter.accepts(persisted):
                raise InvalidCommandError("managed interaction recovery has no matching waiter")
            if persisted.status != "resolved":
                raise InvalidCommandError("managed interaction was cancelled or expired")
            await self._deliver_committed_managed_interaction(persisted, waiter)
            execution.managed_interaction_waiters.pop(record.interaction_id, None)
            delivered = True
        # A preceding reply can have resumed the durable run while this
        # stream still holds its earlier WAITING snapshot. Read it again for
        # each new interaction, including input following an approval.
        latest = await self._store.load_run(run.run_id)
        if latest is None:
            raise InvalidCommandError("interaction run no longer exists")
        if delivered:
            if latest.state is RunState.RUNNING:
                return latest
            return await self._store.save_run_transition(
                latest.model_copy(update={"state": RunState.RUNNING}),
                expected_fence=guard.fencing_token,
            )
        if latest.state is RunState.WAITING:
            return latest
        update = {"state": RunState.WAITING}
        if (
            provider_id == "managed-a2a" and execution.handle.native_ref.get("managed_a2a_takeover")
        ) or (
            provider_id == "langgraph"
            and native_target.get("checkpoint_id")
            and native_target.get("thread_id")
        ):
            # Native input updates the live handle after start. Persist that
            # checkpoint with WAITING so a new process can attach the same run.
            from ksadk.runtime.executor import handle_digest

            update["metadata"] = {
                **latest.metadata,
                "handle": execution.handle.model_dump(mode="json"),
                "handle_digest": handle_digest(execution.handle),
            }
        return await self._store.save_run_transition(
            latest.model_copy(update=update),
            expected_fence=guard.fencing_token,
        )

    async def _deliver_committed_managed_interaction(self, record, waiter) -> None:
        """Bridge a crash after ledger commit but before the live waiter woke."""
        from ksadk.interaction.contracts import InteractionReceipt

        if self._session_events is None:
            raise InvalidCommandError("managed recovery requires the interaction event log")
        from ksadk.interaction.resolution import find_resolution

        event = await find_resolution(self._session_events, record)
        if event is None:
            raise InvalidCommandError("managed interaction terminal fact is missing")
        response = await self._store.read_resolved_response(record)
        if response is None:
            raise InvalidCommandError("managed interaction private response is missing")
        waiter.deliver(
            InteractionReceipt(
                interaction_id=record.interaction_id,
                revision=record.revision,
                status="resolved",
                outcome=event.payload.get("outcome"),
                event_id=event.event_id,
                accepted_seq=event.seq,
            ),
            response.value,
        )

    async def _consume_owned_stream(
        self,
        execution: ActiveExecution,
        run: RunRecord,
        guard: ActivationWriteGuard,
    ) -> None:
        """消费 run 的事件流并把每个事实落为 family=runtime/v2 事件。

        事件 run_id 统一改写为 durable run_id（adapter 私有 run_id 通过
        RunRecord.metadata.runtime_run_id 记录映射）。stream 自然结束后
        才把 run 收口为 COMPLETED；任何异常原样上抛。
        """

        from ksadk.events.canonical import (
            InteractionRequested,
            InteractionResolved,
            RunCompleted,
            RunInterrupted,
            SourceRef,
        )

        runtime_store = None
        recovery_reconciler = None
        if self._session_events is not None:
            # 延迟导入：ksadk.events 反向依赖 kernel.contracts，避免模块环。
            from ksadk.events.canonical_store import RuntimeEventStore

            runtime_store = RuntimeEventStore(self._session_events, session_id=run.session_id)
            if execution.recovering:
                from ksadk.kernel.recovery_projection import RecoveryEventReconciler

                recovery_reconciler = RecoveryEventReconciler(
                    await runtime_store.list(run.session_id, run_id=run.run_id)
                )
        current_run = run
        terminal_state: RunState | None = None
        last_source: SourceRef | None = None
        native_uncertain = False
        async for event in execution.adapter.stream(execution.handle):
            from ksadk.interaction.native_events import NativeSubmissionProgress

            if isinstance(event, NativeSubmissionProgress):
                if self._mcp_authorization is not None:
                    await self._mcp_authorization.consumed(self, current_run, event, guard)
                current_run = await self._confirm_native_submission(current_run, event, guard)
                native_uncertain = any(item.status == "unknown" for item in event._outcomes)
                continue
            if event.event_type == "run.failed" and not native_uncertain:
                current_run, native_uncertain = await self._preserve_unconfirmed_native_failure(
                    current_run, event, guard
                )
            if native_uncertain and event.event_type == "run.failed":
                from ksadk.events.canonical import RunProgress

                # The stream failed, but consumption is unresolved. Preserve
                # the reservation and ownership instead of admitting a fresh
                # business run or replaying a potentially executed tool.
                event = RunProgress(
                    schema_version=2,
                    event_id=event.event_id + ":uncertain",
                    seq=event.seq,
                    timestamp=event.timestamp,
                    run_id=event.run_id,
                    scope_id=event.scope_id,
                    source=event.source.model_copy(
                        update={
                            "metadata": {
                                **event.source.metadata,
                                "native_reconciliation_required": current_run.metadata.get(
                                    "native_reconciliation_required", []
                                ),
                            }
                        }
                    ),
                    status="running",
                    message="Submission outcome requires reconciliation",
                )
            if isinstance(event, InteractionRequested):
                current_run = await self._record_interaction_request(
                    execution, current_run, event, guard
                )
                # The ledger's interaction/v1 SessionEvent is the single fact
                # for this request.  Do not append a second runtime/v2 copy.
                continue
            if isinstance(event, InteractionResolved):
                # The submitted command's ledger transition is first-wins and
                # already emitted interaction.resolved; ignore framework echo.
                continue
            if (
                isinstance(event, RunInterrupted)
                and event.interaction_id
                and current_run.state is RunState.WAITING
            ):
                # Framework adapters emit this after InteractionRequested to
                # describe a temporarily blocked native turn.  The durable
                # Kernel state for that condition is WAITING and the
                # Interaction/v1 ledger is its authority.  Treating the
                # companion run.interrupted event as a terminal fact closes the
                # process-local adapter before a human can answer, so the later
                # SubmitInteraction receipt can never resolve.  Do not publish
                # a contradictory terminal runtime event; preserve the live
                # execution until InteractionResolved resumes the same stream.
                if execution.handle.runtime_type != "langgraph":
                    await self._publish_wait_ready(execution, current_run, guard)
                continue
            if runtime_store is None:
                continue
            if event.run_id != current_run.run_id:
                update: dict = {"run_id": run.run_id}
                if getattr(event, "scope_id", None) == f"run:{execution.handle.run_id}":
                    update["scope_id"] = f"run:{run.run_id}"
                event = event.model_copy(update=update)
            last_source = event.source
            events_to_append = (
                recovery_reconciler.accept(event) if recovery_reconciler is not None else (event,)
            )
            for candidate in events_to_append:
                await runtime_store.append(candidate, guard=guard)
            remember = getattr(execution.adapter, "remember_managed_checkpoint", None)
            if callable(remember):
                remember(execution.handle, event)
            terminal_state = {
                "run.completed": RunState.COMPLETED,
                "run.failed": RunState.FAILED,
                "run.canceled": RunState.CANCELLED,
                "run.interrupted": RunState.INTERRUPTED,
            }.get(event.event_type)
            if terminal_state is not None:
                # App-server style providers keep their notification channel
                # open across turns.  A canonical terminal RuntimeEvent closes
                # this run even when the transport itself does not produce
                # EOF; waiting for EOF here leaves the durable run RUNNING and
                # every later FIFO command queued forever.
                break

        if recovery_reconciler is not None and runtime_store is not None:
            for candidate in recovery_reconciler.flush():
                await runtime_store.append(candidate, guard=guard)

        # ``submit_interaction`` may resolve a live provider while this task is
        # blocked in the framework stream.  It transitions the durable run
        # WAITING -> RUNNING, but ``current_run`` above is deliberately a
        # local snapshot used to preserve event ordering.  Refresh it before
        # deciding whether natural stream exhaustion can settle the run;
        # otherwise a Codex approval continuation finishes successfully but
        # remains permanently WAITING because this task still sees its stale
        # pre-response snapshot.
        latest_run = await self._store.find_active_run(run.agent_instance_id, run.session_id)
        if latest_run is not None and latest_run.run_id == current_run.run_id:
            current_run = latest_run

        if current_run.state is RunState.WAITING and not native_uncertain:
            # Native streams end at an interrupt. Advertise the wire boundary
            # after Adapter cleanup, rather than on the first pending item.
            await self._publish_wait_ready(execution, current_run, guard)

        # ``RuntimeAdapter`` is expected to emit a terminal RuntimeEvent, but
        # a number of framework streams naturally exhaust after their last
        # progress/item event.  The Kernel is the lifecycle owner, so it must
        # publish a fenced ``run.completed`` fact before recording COMPLETED.
        # Otherwise foreground callers and Studio SSE wait forever even though
        # the durable RunRecord says completion succeeded.
        if terminal_state is None and current_run.state is not RunState.WAITING:
            terminal_state = RunState.COMPLETED
            if runtime_store is not None:
                source = last_source or SourceRef(
                    framework="ksadk",
                    native_run_id=execution.runtime_run_id,
                )
                await runtime_store.append(
                    RunCompleted(
                        schema_version=2,
                        event_id=f"{current_run.run_id}:kernel-completed",
                        seq=0,
                        timestamp=datetime.now(timezone.utc).timestamp(),
                        run_id=current_run.run_id,
                        scope_id=f"run:{current_run.run_id}",
                        source=source,
                        status="completed",
                        output_refs=(),
                    ),
                    guard=guard,
                )
        if terminal_state is not None:
            # Managed A2A waiters can end on their own deadline without a
            # SubmitInteraction command. A terminal run must not leave their
            # durable approval/input prompts pending in replay. Native provider
            # interactions retain their existing lifecycle.
            now = datetime.now(timezone.utc)
            for record in await self._store.list_pending_interactions(
                str(current_run.metadata.get("tenant_id") or ""), current_run.session_id
            ):
                if (
                    record.run_id != current_run.run_id
                    or record.agent_instance_id != current_run.agent_instance_id
                    or record.provider_id != "managed-a2a"
                ):
                    continue
                expired = (
                    record.expires_at is not None
                    and datetime.fromisoformat(record.expires_at) <= now
                )
                settle = self._store.expire if expired else self._store.cancel
                try:
                    await settle(record.interaction_id, record.revision, guard=guard)
                except InvalidCommandError as error:
                    if error.details.get("reason") not in {ALREADY_RESOLVED, REVISION_MISMATCH}:
                        raise
                    # Submission can win after the pending snapshot. Only a
                    # committed successor of this exact interaction closes the
                    # race; a revision conflict alone is not terminal evidence.
                    current = await self._store.get(
                        record.interaction_id, tenant_id=record.tenant_id,
                        agent_instance_id=record.agent_instance_id,
                        session_id=record.session_id, run_id=record.run_id,
                    )
                    if (
                        current is None
                        or current.status not in {"resolved", "cancelled", "expired"}
                        or current.revision != record.revision + 1
                        or any(getattr(current, key) != getattr(record, key) for key in (
                            "interaction_id", "tenant_id", "agent_instance_id", "session_id",
                            "run_id", "kind", "provider_id", "native_target",
                        ))
                    ):
                        raise
                waiter = execution.managed_interaction_waiters.pop(record.interaction_id, None)
                if waiter is not None:
                    waiter.close()
            current_run = await self._store.save_run_transition(
                current_run.model_copy(update={"state": terminal_state}),
                expected_fence=guard.fencing_token,
            )
        current = self._executions.get(execution.durable_run_id)
        if (
            terminal_state is not None
            and current is not None
            and current.handle == execution.handle
        ):
            self._executions.pop(execution.durable_run_id, None)
        if terminal_state is not None:
            # Each enqueue owns the adapter instance created in ``_start_run``.
            # Once its stream is terminal there is no live interaction left to
            # preserve, so release the provider transport as part of that same
            # lifecycle.  Codex otherwise leaves one app-server child alive per
            # turn; a later process trying to resume the persisted thread can
            # then block behind the stale owner indefinitely.
            try:
                await execution.adapter.close(execution.handle)
            except Exception:  # noqa: BLE001
                # The durable terminal event and RunRecord are already fenced
                # and committed.  A transport cleanup failure is observable but
                # must not rewrite a successful run into a retryable command.
                logger.exception(
                    "failed to close terminal runtime transport for durable run %s",
                    execution.durable_run_id,
                )
