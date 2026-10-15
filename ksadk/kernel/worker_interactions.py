"""Record worker interactions and deliver committed managed requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ksadk.kernel.worker import ActiveExecution

from datetime import datetime, timezone

from ksadk.kernel.approval_presentation import approval_presentation
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.errors import InvalidCommandError
from ksadk.kernel.state import RunState
from ksadk.kernel.store import RunRecord


class _WorkerInteractionMixin:
    async def _record_interaction_request(
        self,
        execution: ActiveExecution,
        run: RunRecord,
        event: object,
        guard: ActivationWriteGuard,
    ) -> RunRecord:
        """Persist one framework interaction as the durable ledger authority."""

        from ksadk.events.canonical import ApprovalRequest, InteractionRequested
        from ksadk.interaction.contracts import InteractionRecord

        assert isinstance(event, InteractionRequested)
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
        cursor = 0
        while True:
            events = await self._session_events.read(record.session_id, cursor, 256)
            if not events:
                raise InvalidCommandError("managed interaction terminal fact is missing")
            for event in events:
                payload = event.payload
                if (
                    event.family == "interaction"
                    and event.family_version == 1
                    and event.event_type == "interaction.resolved"
                    and event.run_id == record.run_id
                    and all(
                        payload.get(key) == getattr(record, key)
                        for key in (
                            "interaction_id",
                            "tenant_id",
                            "agent_instance_id",
                            "session_id",
                            "run_id",
                            "kind",
                            "revision",
                        )
                    )
                ):
                    waiter.deliver(
                        InteractionReceipt(
                            interaction_id=record.interaction_id,
                            revision=record.revision,
                            status="resolved",
                            outcome=payload.get("outcome"),
                            event_id=event.event_id,
                            accepted_seq=event.seq,
                        ),
                        payload.get("response"),
                    )
                    return
            next_cursor = max(event.seq for event in events)
            if next_cursor <= cursor:
                raise InvalidCommandError("managed interaction recovery cursor is invalid")
            cursor = next_cursor
