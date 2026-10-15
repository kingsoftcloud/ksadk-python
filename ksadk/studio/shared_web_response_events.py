"""Project committed Studio run events onto the Responses stream."""

from __future__ import annotations

from typing import Any

from ksadk.studio import shared_web_run_state as run_state

_APPROVAL_DECISIONS = {
    "approve": "approved",
    "approved": "approved",
    "approved_session": "approved",
    "allow": "approved",
    "accept": "approved",
    "accepted": "approved",
    "reject": "rejected",
    "rejected": "rejected",
    "deny": "rejected",
    "denied": "rejected",
    "decline": "rejected",
    "cancel": "cancelled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}


async def project_response_events(
    self,
    *,
    session_id: str,
    agent_id: str,
    invocation_id: str,
    seen: set[tuple[str, int]],
) -> list[tuple[str, dict[str, Any]]]:
    projected: list[tuple[str, dict[str, Any]]] = []
    current_run_id = self._run_ids_by_invocation.get(invocation_id)
    if not current_run_id:
        return projected
    for run in self.studio.event_store.list_runs(session_id=session_id):
        if run.agent_id != agent_id or run.id != current_run_id:
            continue
        control = run_state.control_ready_event(self.studio, run.id, invocation_id)
        if control and (run.id, -1) not in seen:
            projected.append(control)
            seen.add((run.id, -1))
        events = await self.studio.run_service.events(run.id)
        starts = {
            str(event.data.get("callId") or event.data.get("call_id") or ""): event.data
            for event in events
            if event.type in {"command.started", "tool.started", "tool.requested"}
        }
        for event in events:
            key = (run.id, event.id)
            if key in seen:
                continue
            seen.add(key)
            if event.type == "message.completed":
                native = event.data.get("runtimeEvent") or {}
                metadata = native.get("source", {}).get("metadata", {})
                if (metadata.get("engine_kind") == "native"
                        and metadata.get("native_event_kind") == "effect.received"
                        and metadata.get("provisional") is True
                        and metadata.get("provisional_retired") is not True):
                    # Native retains a completed draft only when the sealed
                    # model turn contains tool calls. Publish that commentary
                    # once; rejected/truncated/final previews remain in trace.
                    text = str(event.data.get("text") or "")
                    if text:
                        name = "response.output_text.delta"
                        projected.append((name, self._response_delta_payload(
                            name, invocation_id=invocation_id, delta=text,
                        )))
                continue
            if event.type in {"message.delta", "thinking.delta"}:
                delta = str(event.data.get("text") or event.data.get("delta") or "")
                if not delta:
                    continue
                native = event.data.get("runtimeEvent") or {}
                metadata = native.get("source", {}).get("metadata", {})
                if (metadata.get("engine_kind") == "native"
                        and metadata.get("provisional") is True):
                    # The published Responses client appends text immediately;
                    # it cannot retract a draft when the canonical item is
                    # retired after an output limit or rejected candidate.
                    # Preserve draft observations in the native ledger and
                    # trace; sealed tool commentary and final delivery have
                    # distinct completion events below/above this branch.
                    # Never infer reasoning from prose or relabel it as such.
                    continue
                is_activity = run.runtime_type == "harness" and (
                    metadata.get("native_event_type", metadata.get("native_event_kind"))
                    == "run.progress"
                )
                event_name = (
                    "response.output_text.delta"
                    if event.type == "message.delta" and not is_activity
                    else "response.reasoning_summary_text.delta"
                )
                projected.append(
                    (
                        event_name,
                        self._response_delta_payload(
                            event_name,
                            invocation_id=invocation_id,
                            delta=f"\n{delta}\n" if is_activity else delta,
                        ),
                    )
                )
                continue
            if event.type.startswith("a2ui."):
                projected.append(
                    (
                        event.type,
                        {
                            "type": event.type,
                            "runId": run.id,
                            **event.data,
                        },
                    )
                )
                continue
            if event.type == "run.paused":
                projected.append(
                    (
                        "response.paused",
                        {
                            "type": "response.paused",
                            "response_id": invocation_id,
                            "runId": run.id,
                        },
                    )
                )
                continue
            if event.type == "run.resumed":
                projected.append(
                    (
                        "response.resumed",
                        {
                            "type": "response.resumed",
                            "response_id": invocation_id,
                            "runId": run.id,
                        },
                    )
                )
                continue
            if event.type == "approval.resolved":
                # Older projections lost the top-level decision but retained
                # the canonical response. Missing evidence is never approval.
                runtime_response = (event.data.get("runtimeEvent") or {}).get("response") or {}
                raw_decision = str(
                    event.data.get("decision")
                    or runtime_response.get("decision")
                    or event.data.get("outcome")
                    or event.data.get("name")
                    or ""
                ).strip().lower()
                decision = _APPROVAL_DECISIONS.get(raw_decision)
                if decision is None:
                    # The legacy Web decoder treats any unknown decision as
                    # approved. Retain the audit event without emitting a
                    # resolution that this client cannot represent safely.
                    continue
                projected.append(
                    (
                        "response.ksadk.approval_resolved",
                        {
                            "type": "response.ksadk.approval_resolved",
                            "approvalRequestId": event.data.get("approvalId")
                            or event.data.get("interactionId"),
                            "decision": decision,
                            "revision": event.data.get("revision") or 2,
                            "runId": run.id,
                        },
                    )
                )
                continue
            item_event = self._response_item_event(event.type, event.data, starts)
            if item_event is not None:
                projected.append(item_event)
    return projected
