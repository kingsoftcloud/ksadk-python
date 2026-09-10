"""Project Skill disclosure events into feedback suitable for Studio/Skill Service.

The Harness does not install, publish or rank marketplace Skills.  It does own the
runtime evidence: which bound Skill was recommended, which levels the model loaded,
and whether disclosure calls failed.  This module exposes that evidence without
including Skill content, tool results or user prompts.
"""

from __future__ import annotations

from typing import Any, Sequence

from ksadk.harness.events import EventType, RuntimeEvent

_SKILL_TOOLS = {
    "skill_read_manifest": 1,
    "skill_read_instructions": 2,
    "skill_read_resource": 3,
}


def skill_feedback(
    events: Sequence[RuntimeEvent],
    *,
    bound_skill_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """Return metadata-only disclosure and failure metrics grouped by Skill ref."""

    state: dict[str, dict[str, Any]] = {
        ref: _new_item(ref) for ref in dict.fromkeys(bound_skill_refs) if ref
    }
    begins: dict[str, tuple[str, str]] = {}

    for event in sorted(events, key=lambda item: (item.timestamp, item.seq_id)):
        payload = event.payload
        if event.event_type == EventType.TOOL_CALL_BEGIN:
            name = str(payload.get("name") or "")
            if name not in _SKILL_TOOLS:
                continue
            args = payload.get("args") or payload.get("arguments") or {}
            skill_ref = str(args.get("skill_id") or "") if isinstance(args, dict) else ""
            begins[str(payload.get("call_id") or "")] = (name, skill_ref)
            if skill_ref:
                item = state.setdefault(skill_ref, _new_item(skill_ref))
                item["disclosure_calls"] += 1
            continue

        if event.event_type == EventType.TOOL_CALL_END:
            call_id = str(payload.get("call_id") or "")
            name, skill_ref = begins.pop(
                call_id,
                (str(payload.get("name") or ""), ""),
            )
            if name not in _SKILL_TOOLS or not skill_ref:
                continue
            item = state.setdefault(skill_ref, _new_item(skill_ref))
            if payload.get("error"):
                item["failed_calls"] += 1
            else:
                item["successful_calls"] += 1
            continue

        if event.event_type != EventType.SKILL_DISCLOSED:
            continue
        skill_ref = str(payload.get("skill_ref") or "")
        if not skill_ref:
            continue
        item = state.setdefault(skill_ref, _new_item(skill_ref))
        level = int(payload.get("level") or 0)
        if level in {1, 2, 3} and level not in item["levels_reached"]:
            item["levels_reached"].append(level)
            item["levels_reached"].sort()
        item["max_level"] = max(item["max_level"], level)
        item["recommended"] = item["recommended"] or bool(payload.get("recommended"))
        item["disclosed_bytes"] += max(0, int(payload.get("size_bytes") or 0))
        item["resource_reads"] += int(level == 3)

    items = []
    for skill_ref in sorted(state):
        item = state[skill_ref]
        item["used"] = item["max_level"] > 0
        item["completed_progression"] = item["levels_reached"] == [1, 2, 3]
        item["invalid_call_rate"] = _ratio(
            item["failed_calls"], item["successful_calls"] + item["failed_calls"]
        )
        item["deep_disclosure_without_resource"] = (
            item["max_level"] == 2 and item["resource_reads"] == 0
        )
        items.append(item)

    return {
        "schemaVersion": 1,
        "boundCount": len(tuple(dict.fromkeys(ref for ref in bound_skill_refs if ref))),
        "observedCount": len(items),
        "usedCount": sum(1 for item in items if item["used"]),
        "recommendedUsedCount": sum(
            1 for item in items if item["used"] and item["recommended"]
        ),
        "failedCallCount": sum(item["failed_calls"] for item in items),
        "items": items,
    }


def _new_item(skill_ref: str) -> dict[str, Any]:
    return {
        "skill_ref": skill_ref,
        "recommended": False,
        "used": False,
        "levels_reached": [],
        "max_level": 0,
        "disclosure_calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "resource_reads": 0,
        "disclosed_bytes": 0,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


__all__ = ["skill_feedback"]
