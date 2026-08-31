"""Deterministic background maintenance for session-scoped Working Context.

Maintenance produces a versioned :class:`WorkingContextPatch`; callers decide when
to apply it.  It never edits the Stable Prompt, never writes long-term Memory and
never retries a version conflict by silently overwriting newer state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ksadk.harness.state import WorkingContext
from ksadk.harness.working_context_patch import PatchOperation, WorkingContextPatch

_FIELD_LIMITS = {
    "confirmed_constraints": 64,
    "open_questions": 64,
    "verified_facts": 32,
    "recent_tool_failures": 8,
}


@dataclass(frozen=True)
class ContextMaintenancePlan:
    patch: WorkingContextPatch | None
    before_items: int
    after_items: int
    removed_empty: int
    removed_duplicates: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "baseVersion": self.patch.base_version if self.patch else None,
            "changed": self.patch is not None,
            "beforeItems": self.before_items,
            "afterItems": self.after_items,
            "removedEmpty": self.removed_empty,
            "removedDuplicates": self.removed_duplicates,
            "patch": self.patch.to_payload() if self.patch else None,
        }


def plan_context_maintenance(
    working: WorkingContext,
    *,
    source_event_ids: Sequence[str] = (),
) -> ContextMaintenancePlan:
    """Create a bounded, deterministic cleanup patch for tuple fields."""

    operations: list[PatchOperation] = []
    before_items = 0
    after_items = 0
    removed_empty = 0
    removed_duplicates = 0
    for field_name, limit in _FIELD_LIMITS.items():
        original = tuple(getattr(working, field_name))
        before_items += len(original)
        cleaned, empty_count, duplicate_count = _clean(original, limit=limit)
        after_items += len(cleaned)
        removed_empty += empty_count
        removed_duplicates += duplicate_count
        if cleaned != original:
            operations.append(
                PatchOperation(op="set", field_name=field_name, value=cleaned)
            )

    patch = None
    if operations:
        patch = WorkingContextPatch(
            base_version=working.version,
            operations=tuple(operations),
            source_event_ids=tuple(dict.fromkeys(item for item in source_event_ids if item)),
            reason="background_context_maintenance",
        )
    return ContextMaintenancePlan(
        patch=patch,
        before_items=before_items,
        after_items=after_items,
        removed_empty=removed_empty,
        removed_duplicates=removed_duplicates,
    )


def _clean(values: tuple[str, ...], *, limit: int) -> tuple[tuple[str, ...], int, int]:
    normalized: list[str] = []
    keys: set[str] = set()
    removed_empty = 0
    removed_duplicates = 0
    # Keep the newest spelling/value when the same normalized item occurs repeatedly.
    for raw in reversed(values):
        value = " ".join(str(raw).split())
        if not value:
            removed_empty += 1
            continue
        key = value.casefold()
        if key in keys:
            removed_duplicates += 1
            continue
        keys.add(key)
        normalized.append(value)
    cleaned = tuple(reversed(normalized))[-limit:]
    return cleaned, removed_empty, removed_duplicates


__all__ = ["ContextMaintenancePlan", "plan_context_maintenance"]
