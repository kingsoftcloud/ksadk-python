"""AG-UI Agent duck seam backed exclusively by a KSADK RuntimeAdapter."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from ag_ui.core import (
    RunAgentInput,
)

from ksadk.agui._agent_types import _ThreadRun
from ksadk.events.canonical import (
    ApprovalResponse,
    InteractionResolved,
    RunCanceled,
    SourceRef,
    StructuredInputResponse,
)
from ksadk.interaction.display_values import submission_display_value
from ksadk.interaction.form_values import normalize_form_answer
from ksadk.runtime.adapter import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
)

logger = logging.getLogger(__name__)


class AguiRootNullUnsupported(ValueError):
    """The formal optional payload cannot carry a root JSON null."""


class AGUIResumeMixin:
    async def _cancel_native_wait(self, input, current, entries):
        """Cancellation closes the logical run; it is never a fabricated answer."""
        accepted = {
            CancelResult.INTERRUPTED_ACTIVE_TURN,
            CancelResult.PENDING_CANCEL_RECORDED,
            CancelResult.NOT_RUNNING,
        }
        current.cancel_failed = True
        current.active = True
        result = await self._shared.executor.cancel(current.handle)
        if result not in accepted:
            raise RuntimeError("Native cancellation was not acknowledged")
        ids = sorted(current.pending)
        key = json.dumps([input.thread_id, current.handle.run_id, "cancel"])
        await self._persist(
            RunCanceled(
                schema_version=2,
                event_id="evt_agui_cancel_" + hashlib.sha256(key.encode()).hexdigest(),
                seq=0,
                timestamp=time.time(),
                run_id=current.handle.run_id,
                scope_id=f"ksadk:{current.handle.run_id}",
                source=SourceRef(
                    framework="ksadk",
                    metadata={
                        "protocol": "ag-ui",
                        "cancelled_interrupt_ids": ids,
                    },
                ),
                status="canceled",
                reason="native_checkpoint_wait_cancelled",
            ),
            session_id=input.thread_id,
        )
        retire_wait = getattr(self._shared.executor, "retire_cancelled_wait", None)
        if callable(retire_wait):
            await retire_wait(current.handle)
        try:
            await self._shared.executor.close(current.handle)
        except Exception as error:
            # The acknowledged cancellation fact is already durable. A
            # transport cleanup failure must not reopen or block that run.
            logger.warning("Cancelled native transport cleanup failed: %s", type(error).__name__)
        current.cancelled, current.interrupted = True, False
        current.cancel_failed, current.active = False, False
        current.pending.clear()
        if self._shared.threads.get(input.thread_id) is current:
            self._shared.threads.pop(input.thread_id)
        for entry in entries:
            self._shared.consumed_resumes[(input.thread_id, entry.interrupt_id)] = (
                self._resume_fingerprint(entry.status, entry.payload)
            )
        return current, False

    async def _record_native_resume_outcomes(self, input, run, event):
        for outcome in event._outcomes:
            entry = run.native_submissions.get(outcome.interaction_id)
            if entry is None:
                continue
            if outcome.status == "consumed":
                await self._persist_resolved_approvals(input, run, [entry])
                self._shared.consumed_resumes[(input.thread_id, outcome.interaction_id)] = (
                    self._resume_fingerprint(entry.status, entry.payload)
                )
                run.pending.pop(outcome.interaction_id, None)
            if outcome.status in {"consumed", "rejected"}:
                run.native_submissions.pop(outcome.interaction_id, None)
            else:
                run.interrupted = True

    async def _resume_locked(self, input: RunAgentInput) -> tuple[_ThreadRun, bool]:
        entries = input.resume or []
        ids = [entry.interrupt_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("resume contains duplicate interruptId values")

        current = self._shared.threads.get(input.thread_id)
        if current is not None and (current.active or current.native_submissions):
            raise ValueError("The original run is still processing a submission")
        if current is None or not current.interrupted:
            consumed_fingerprints = {
                entry.interrupt_id: self._resume_fingerprint(entry.status, entry.payload)
                for entry in entries
            }
            if consumed_fingerprints and all(
                self._shared.consumed_resumes.get((input.thread_id, interrupt_id)) == fingerprint
                for interrupt_id, fingerprint in consumed_fingerprints.items()
            ):
                return self._duplicate_run(input), True
            current, duplicate = await self._restore_durable_run(input, consumed_fingerprints)
            if duplicate:
                return self._duplicate_run(input), True
            if current is None:
                raise ValueError(f"thread {input.thread_id!r} has no resumable run")
            self._shared.threads[input.thread_id] = current

        pending_ids = set(current.pending)
        native = bool(current.handle.native_ref.get("native_waits"))
        if not set(ids).issubset(pending_ids) if native else set(ids) != pending_ids:
            missing = sorted(pending_ids - set(ids))
            unknown = sorted(set(ids) - pending_ids)
            raise ValueError(
                f"resume does not match pending interrupts; missing={missing}, unknown={unknown}"
            )
        if native and any(entry.status == "cancelled" for entry in entries):
            return await self._cancel_native_wait(input, current, entries)

        checkpoint_ids = {pending.checkpoint_id for pending in current.pending.values()}
        if len(checkpoint_ids) != 1 or not next(iter(checkpoint_ids), ""):
            raise ValueError("pending interrupts do not share a resumable checkpoint")

        decisions: dict[str, Any] = {}
        fingerprints: dict[str, Any] = {}
        for entry in entries:
            payload = entry.payload if entry.status == "resolved" else {"type": "reject"}
            if native:
                pending = current.pending[entry.interrupt_id]
                if pending.kind == "structured_input":
                    if entry.status != "resolved":
                        raise ValueError(
                            "Cancel a structured input through the Runtime cancel control"
                        )
                    if "payload" not in entry.model_fields_set:
                        from ksadk.interaction.schema import InputValidationError

                        raise InputValidationError(
                            [
                                {
                                    "path": "payload",
                                    "keyword": "required",
                                    "message": "A structured answer must be explicitly provided",
                                }
                            ]
                        )
                    if entry.payload is None:
                        raise AguiRootNullUnsupported("AG-UI root null requires Interaction/v1")
                    payload = normalize_form_answer(
                        pending.interrupt.response_schema or {}, entry.payload
                    )
            decisions[entry.interrupt_id] = payload
            fingerprints[entry.interrupt_id] = self._resume_fingerprint(entry.status, entry.payload)

        single_id = ids[0] if len(ids) == 1 else None
        payload_data = decisions[single_id] if single_id is not None else decisions
        if not native:
            reservation_created = await self._persist_resolved_approvals(input, current, entries)
            if not reservation_created:
                return self._duplicate_run(input), True
            for interrupt_id, fingerprint in fingerprints.items():
                self._shared.consumed_resumes[(input.thread_id, interrupt_id)] = fingerprint

        resumed = await self._shared.executor.resume(
            current.handle,
            ResumeTarget(kind="checkpoint_id", id=next(iter(checkpoint_ids))),
            ResumePayload(
                kind="approval_decision",
                call_id=single_id,
                data=payload_data,
            ),
        )
        current.handle = resumed
        current.interrupted = False
        if native:
            current.native_submissions = {entry.interrupt_id: entry for entry in entries}
        else:
            current.pending.clear()
        return current, False

    async def _persist_resolved_approvals(
        self,
        input: RunAgentInput,
        run: _ThreadRun,
        entries: list[Any],
    ) -> bool:
        if self._shared.event_store_factory is None:
            return True
        reservation_created = True
        for index, entry in enumerate(entries):
            fingerprint = self._resume_fingerprint(entry.status, entry.payload)
            event_key = json.dumps(
                [input.thread_id, entry.interrupt_id],
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            decision = self._approval_decision_for_audit(entry.status, entry.payload)
            pending = run.pending.get(entry.interrupt_id)
            kind = pending.kind if pending is not None else "approval"
            display_answer = entry.payload
            if kind == "structured_input" and entry.interrupt_id in run.native_submissions:
                display_answer = normalize_form_answer(
                    pending.interrupt.response_schema or {}, entry.payload
                )
            event = InteractionResolved(
                schema_version=2,
                event_id=f"evt_agui_resume_{hashlib.sha256(event_key.encode()).hexdigest()}",
                seq=0,
                timestamp=time.time(),
                run_id=run.handle.run_id,
                scope_id=f"ksadk:{run.handle.run_id}",
                source=SourceRef(
                    framework="ksadk",
                    metadata={
                        "agent_id": str(run.handle.native_ref.get("agent_id") or self.name),
                        "user_id": str(run.handle.native_ref.get("user_id") or "agui-user"),
                        "session_id": input.thread_id,
                        "protocol": "ag-ui",
                        "resume_fingerprint": fingerprint,
                    },
                ),
                interaction_id=entry.interrupt_id,
                interaction_kind=kind,
                response=(
                    StructuredInputResponse(
                        response_type="structured_input",
                        data=submission_display_value(
                            pending.interrupt.response_schema or {},
                            display_answer,
                        ),
                    )
                    if kind == "structured_input"
                    else ApprovalResponse(
                        response_type="approval",
                        decision=(
                            decision
                            if decision in ("approved", "rejected", "canceled")
                            else "rejected"
                        ),
                        data={"call_id": entry.interrupt_id},
                    )
                ),
            )
            if index == 0:
                _persisted, reservation_created = await self._reserve(
                    event, session_id=input.thread_id
                )
                if not reservation_created:
                    return False
            else:
                await self._persist(event, session_id=input.thread_id)
        return reservation_created
