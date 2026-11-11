"""Publish native waits only from a drained, persisted root snapshot."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ksadk.events.canonical import (
    ApprovalRequest,
    ContinuationCreated,
    StructuredInputRequest,
)
from ksadk.events.identity import stable_event_id, stable_item_id
from ksadk.interaction.langgraph_observer import collect_native_waits
from ksadk.interaction.native_events import NativeInteractionRequested, NativeSubmissionProgress


def submission_progress(context, scope_id, source, observer):
    if observer is None:
        return None
    outcomes = observer.outcomes()
    attempt = observer.attempt_key
    event = NativeSubmissionProgress(
        schema_version=2,
        event_id=stable_event_id(
            "langgraph", scope_id, attempt, "run.progress", "native-consumption", attempt, 0
        ),
        seq=context.allocate_placeholder_seq(),
        timestamp=time.time(),
        run_id=context.run_id,
        scope_id=scope_id,
        source=source,
        status="running",
        message="Native submission processed",
    )
    event._outcomes = outcomes
    return event


async def live_resume_config(runner, config, resume_context):
    """Resume the verified latest wait, rather than fork a historical replay.

    LangGraph replays successful parallel tasks when an explicit historical
    checkpoint ID is supplied. Check the saved identity against the latest
    root and its actual leaf tasks before using the public live-resume API.
    This is limited to SDK-owned interaction submissions; time travel keeps
    its explicit checkpoint and existing semantics.
    """
    expected = config.get("configurable") or {}
    latest = {**config, "configurable": dict(expected)}
    latest["configurable"].pop("checkpoint_id", None)
    snapshot, error = await runner._read_graph_state(latest, include_subgraphs=True)
    root = (getattr(snapshot, "config", {}) or {}).get("configurable") or {}
    if (
        error
        or not expected.get("checkpoint_id")
        or any(
            str(root.get(key) or "") != str(expected.get(key) or "")
            for key in ("thread_id", "checkpoint_id", "checkpoint_ns")
        )
    ):
        raise ValueError("Native interaction checkpoint changed; reconciliation is required")
    waits = {(wait.task_id, wait.interrupt.native_id) for wait in collect_native_waits(snapshot)}
    if any(
        (target["task_id"], target["call_id"]) not in waits
        for target in resume_context["submitted"].values()
    ):
        raise ValueError("Native interaction is no longer waiting at its recorded task")
    return latest


async def persisted_interactions(runner, config, context, scope_id, source, resume_context):
    # Explicit checkpoint IDs select history. Read the latest root after the
    # invocation has drained, otherwise a resumed parent can still appear to
    # contain the old wait while its child is finishing a checkpoint write.
    latest = {**config, "configurable": dict(config.get("configurable") or {})}
    latest["configurable"].pop("checkpoint_id", None)
    snapshot, error = await runner._read_graph_state(latest, include_subgraphs=True)
    if error:
        raise RuntimeError("Native interaction checkpoint could not be read") from error
    waits = collect_native_waits(snapshot)
    if not waits:
        return []
    root = dict((getattr(snapshot, "config", {}) or {}).get("configurable") or {})
    checkpoint_id = root.get("checkpoint_id")
    if not checkpoint_id:
        raise RuntimeError("Native interaction has no persisted root checkpoint")
    ref = {key: str(root.get(key) or "") for key in ("thread_id", "checkpoint_id", "checkpoint_ns")}
    continuation_id = stable_item_id(
        "langgraph", scope_id, "continuation", *(f"{k}:{v}" for k, v in ref.items())
    )
    capability = runner.describe_checkpoint_capability()
    created_at = getattr(snapshot, "created_at", None)
    checkpoint_time = datetime.fromisoformat(created_at).timestamp() if created_at else time.time()
    continuation = ContinuationCreated(
        schema_version=2,
        event_id=stable_event_id(
            "langgraph",
            scope_id,
            continuation_id,
            "continuation.created",
            "checkpoint",
            checkpoint_id,
            0,
        ),
        seq=context.allocate_placeholder_seq(),
        timestamp=checkpoint_time,
        run_id=context.run_id,
        scope_id=scope_id,
        source=source.model_copy(
            update={
                "metadata": {
                    **source.metadata,
                    "checkpoint": True,
                    "capability": {
                        "backend": capability.get("Backend", "unknown"),
                        "scope": capability.get("Scope", "unknown"),
                        "durable": bool(capability.get("Durable")),
                        "is_resumable": True,
                        "is_terminal": False,
                        "resume_status": "resumable",
                    },
                }
            }
        ),
        continuation_id=continuation_id,
        continuation_kind="graph_checkpoint",
        resumable=True,
        ref=ref,
    )
    prior = resume_context.get("pending", {})
    consumed = set(resume_context.get("consumed", ()))
    events: list[Any] = [continuation]
    for wait in waits:
        native = wait.interrupt
        previous = next(
            (
                (key, value)
                for key, value in prior.items()
                if value.get("call_id") == native.native_id and value.get("task_id") == wait.task_id
            ),
            None,
        )
        generation = int(previous[1].get("generation", 0)) if previous else 0
        if previous and previous[0] in consumed:
            generation += 1
        public_id = (
            previous[0]
            if previous and previous[0] not in consumed
            else stable_item_id(
                "langgraph", context.run_id, "wait", native.native_id, wait.task_id, str(generation)
            )
        )
        requested_at = (
            previous[1]["requested_at"] if previous and previous[0] == public_id else time.time()
        )
        request = (
            StructuredInputRequest(prompt=native.prompt, schema=native.schema)
            if native.kind == "structured_input"
            else ApprovalRequest(call_id=public_id, kind="tool", detail=native.value)
        )
        event = NativeInteractionRequested(
            schema_version=2,
            event_id=stable_event_id(
                "langgraph", scope_id, public_id, "interaction.requested", "request", public_id, 0
            ),
            seq=context.allocate_placeholder_seq(),
            timestamp=requested_at,
            run_id=context.run_id,
            scope_id=scope_id,
            source=source,
            interaction_id=public_id,
            interaction_kind=native.kind,
            request=request,
        )
        event._native_target = {
            **ref,
            "call_id": native.native_id,
            "task_id": wait.task_id,
            "task_namespace": wait.task_namespace,
            "generation": generation,
            "requested_at": requested_at,
        }
        if native.kind == "approval":
            from ksadk.interaction.langgraph_approval import approval_contract

            contract = approval_contract(native.value)
            if contract is not None:
                event._native_target["approval_contract"] = contract
        event._presentation = native.presentation
        if (isinstance(native.value, dict) and isinstance(native.value.get("surface_id"), str)
            and "presentation" not in native.value):
            event._native_target["a2ui_surface_id"] = native.value["surface_id"]
        events.append(event)
    return events


__all__ = ["live_resume_config", "persisted_interactions", "submission_progress"]
