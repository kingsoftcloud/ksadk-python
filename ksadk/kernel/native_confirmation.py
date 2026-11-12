"""Apply native consumption evidence to the existing Interaction/v1 ledger."""

from ksadk.interaction.submission import reserved_submission
from ksadk.kernel.state import RunState
from ksadk.kernel.store import control_event


class NativeConfirmationMixin:
    async def _preserve_unconfirmed_native_failure(self, run, event, guard):
        from ksadk.interaction.native_events import (
            NativeSubmissionOutcome,
            NativeSubmissionProgress,
        )

        outcomes = []
        for record in await self._store.list_pending_interactions(
            str(run.metadata.get("tenant_id") or ""), run.session_id
        ):
            if (
                record.run_id == run.run_id
                and record.agent_instance_id == run.agent_instance_id
                and record.provider_id == "langgraph"
                and record.status == "resolving"
                and (record.native_target or {}).get("task_id")
            ):
                submission, _ = reserved_submission(record)
                outcomes.append(
                    NativeSubmissionOutcome(
                        record.interaction_id, submission.idempotency_key, "unknown"
                    )
                )
        extension = getattr(self, "_mcp_authorization", None)
        if extension is not None:
            for row in await extension.pending(run.run_id):
                if row["status"] == "resolving":
                    outcomes.append(NativeSubmissionOutcome(row["interaction_id"], row["attempt_key"], "unknown"))
        if not outcomes:
            return run, False
        unknown = NativeSubmissionProgress(
            schema_version=2,
            event_id=event.event_id + ":native-outcome",
            seq=event.seq,
            timestamp=event.timestamp,
            run_id=event.run_id,
            scope_id=event.scope_id,
            source=event.source,
            status="running",
        )
        unknown._outcomes = tuple(outcomes)
        if extension is not None:
            await extension.consumed(self, run, unknown, guard)
        return await self._confirm_native_submission(run, unknown, guard), True

    async def _confirm_native_submission(self, run, event, guard):
        for outcome in event._outcomes:
            record = await self._store.get(
                outcome.interaction_id,
                tenant_id=str(run.metadata.get("tenant_id") or ""),
                agent_instance_id=run.agent_instance_id,
                session_id=run.session_id,
                run_id=run.run_id,
            )
            if record is None or record.status != "resolving":
                continue
            submission, attempt = reserved_submission(record)
            if outcome.attempt_key != submission.idempotency_key:
                continue
            if outcome.status == "consumed":
                await self._store.resolve(submission, guard=guard)
            elif outcome.status == "rejected":
                await self._store.release_submission(submission, guard=guard)
                await self._store.append_event(
                    control_event(
                        session_id=run.session_id,
                        event_type="control.command_rejected",
                        payload={
                            "command_id": attempt["command_id"],
                            "status": "rejected",
                            "reason": "input_response_invalid",
                            "interaction_id": outcome.interaction_id,
                        },
                    ),
                    expected_fence=guard.fencing_token,
                    agent_instance_id=run.agent_instance_id,
                )
            # Unknown consumption intentionally stays resolving for recovery
            # reconciliation. Replaying a potentially executed tool is unsafe.
        latest = await self._store.load_run(run.run_id)
        if latest is not None and any(item.status == "unknown" for item in event._outcomes):
            latest = await self._store.save_run_transition(
                latest.model_copy(
                    update={
                        "state": RunState.WAITING,
                        "metadata": {
                            **latest.metadata,
                            "native_reconciliation_required": [
                                item.interaction_id
                                for item in event._outcomes
                                if item.status == "unknown"
                            ],
                        },
                    }
                ),
                expected_fence=guard.fencing_token,
            )
        return latest or run


__all__ = ["NativeConfirmationMixin"]
