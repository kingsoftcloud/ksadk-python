"""Pure browser ownership admission; restored pages never inherit old evidence."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import JsonValue

from .browser_contract import BrowserRecovery, BrowserSessionRef
from .state import RunPhase, RunSnapshot
from .transitions import commit_transition, finish

if TYPE_CHECKING:
    from .ports import ToolResult


def remember_browser(
    snapshot: RunSnapshot, ref: BrowserSessionRef, *, closed: bool = False
) -> RunSnapshot:
    if ref.scope != snapshot.scope or ref.run_id != snapshot.run_id:
        return finish(snapshot, "failed", "browser_reference_scope_mismatch")
    if closed:
        # An old session's close must not erase a newer owner/generation.
        return snapshot.model_copy(update={
            "browser_session_refs": tuple(
                item for item in snapshot.browser_session_refs
                if item != ref.model_dump(mode="json")
            ),
        })
    refs = tuple(
        item for item in snapshot.browser_session_refs if item.get("runtime_id") != ref.runtime_id
    )
    if len(refs) >= 16:
        return finish(snapshot, "failed", "browser_reference_limit")
    return snapshot.model_copy(
        update={
            "browser_session_refs": (*refs, ref.model_dump(mode="json")),
        }
    )


def recovery_transition(snapshot: RunSnapshot, reports: tuple[BrowserRecovery, ...]):
    refs = tuple(BrowserSessionRef.model_validate(item) for item in snapshot.browser_session_refs)
    if len(reports) != len(refs):
        raise ValueError("browser recovery must account for every bound reference")
    events = []
    for original, report in zip(refs, reports, strict=True):
        recovered = report.session_ref
        if original.scope != snapshot.scope or original.run_id != snapshot.run_id:
            raise ValueError("persisted browser scope mismatch")
        expected = (
            original
            if report.status != "restarted"
            else original.model_copy(
                update={
                    "generation": original.generation + 1,
                    "lease_epoch": original.lease_epoch + 1,
                }
            )
        )
        if recovered != expected:
            raise ValueError("browser recovery reference mismatch")
        events.append(
            (
                "browser.recovered",
                {
                    "session_id": recovered.session_id,
                    "runtime_id": recovered.runtime_id,
                    "status": report.status,
                    "generation": recovered.generation,
                    "lease_epoch": recovered.lease_epoch,
                    "last_action_seq": report.last_action_seq,
                    "reason": report.reason,
                    "reobserve_available": report.reobserve_available,
                },
            )
        )
    missing = not refs and snapshot.last_browser_observation is not None
    if missing:
        events.append(
            (
                "browser.recovered",
                {
                    "status": "lost",
                    "reason": "browser_reference_missing",
                },
            )
        )
    if not events:
        return None
    unavailable = missing or any(report.status != "reconnected" for report in reports)
    updated = snapshot
    if unavailable:
        safe_reobserve = (
            not missing
            and snapshot.phase in {RunPhase.PREPARING_CONTEXT, RunPhase.EXECUTING_TOOLS}
            and snapshot.pending_intent is None
            and not snapshot.pending_tools
            and all(
                report.status == "reconnected"
                or (report.status == "lost" and report.reobserve_available)
                for report in reports
            )
        )
        updated = (
            snapshot
            if safe_reobserve
            else finish(snapshot, "unverified", "browser_session_unavailable")
        ).model_copy(
            update={
                "last_browser_observation": None,
                "last_browser_attempt_tool_call": None,
                "prepared_context": None,
                "candidate": None,
                "verification": None,
                "verification_tool_calls": None,
                "delivery_review": None,
                "final_text": "",
            }
        )
        if safe_reobserve:
            updated = updated.model_copy(
                update={
                    "phase": RunPhase.PREPARING_CONTEXT,
                    "browser_reobservation_refs": tuple(
                        report.session_ref.model_dump(mode="json")
                        for report in reports
                        if report.status == "lost"
                    ),
                    "messages": (
                        *snapshot.messages,
                        {
                            "role": "user",
                            "kind": "browser_recovery_observation",
                            "content": json.dumps(
                                {
                                    "status": "lost",
                                    "page_continuity": False,
                                    "prior_observation_valid": False,
                                    "fresh_observation_required": True,
                                },
                                sort_keys=True,
                            ),
                        },
                    ),
                }
            )
            events.append(
                (
                    "browser.reobservation_required",
                    {
                        "prior_observation_valid": False,
                        "old_actions_replayed": False,
                    },
                )
            )
    return commit_transition(
        snapshot, updated, events[0][0], events[0][1], extra_events=tuple(events[1:])
    )


def remaining_reobservations(
    snapshot: RunSnapshot,
    result: ToolResult,
    observation: dict[str, JsonValue] | None,
) -> tuple[dict[str, JsonValue], ...]:
    """Only a successful typed replacement observation retires resource debt."""
    ref = result.browser_session_ref
    if (
        result.status != "ok"
        or ref is None
        or observation is None
        or observation.get("tool_call") != snapshot.tool_calls + 1
    ):
        return snapshot.browser_reobservation_refs
    original = ref.model_copy(
        update={
            "generation": ref.generation - 1,
            "lease_epoch": ref.lease_epoch - 1,
        }
    ).model_dump(mode="json")
    return tuple(old for old in snapshot.browser_reobservation_refs if old != original)
