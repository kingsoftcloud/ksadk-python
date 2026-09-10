"""Deterministic background maintenance for session-scoped Working Context.

Maintenance produces a versioned :class:`WorkingContextPatch`; callers decide when
to apply it.  It never edits the Stable Prompt, never writes long-term Memory and
never retries a version conflict by silently overwriting newer state.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, Sequence

from ksadk.harness.state import WorkingContext
from ksadk.harness.working_context_patch import (
    PatchOperation,
    WorkingContextPatch,
    apply_patch,
)

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


class WorkingContextSnapshotStore(Protocol):
    """Checkpoint/control-plane boundary used by the background worker."""

    def load(self, *, tenant_id: str, session_id: str) -> WorkingContext | None: ...

    def compare_and_set(
        self,
        *,
        tenant_id: str,
        session_id: str,
        expected_version: int,
        value: WorkingContext,
    ) -> bool: ...


MaintenanceStatus = Literal[
    "pending", "running", "succeeded", "unchanged", "conflicted", "failed"
]


@dataclass(frozen=True)
class ContextMaintenanceTask:
    task_id: str
    tenant_id: str
    session_id: str
    status: MaintenanceStatus = "pending"
    attempts: int = 0
    before_items: int = 0
    after_items: int = 0
    error_code: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


class ContextMaintenanceQueue:
    """Bounded background organizer with compare-and-set persistence.

    Submitting never performs storage I/O.  A stale task is marked ``conflicted``
    and never retries by overwriting the newer session snapshot.
    """

    def __init__(self, store: WorkingContextSnapshotStore, *, max_pending: int = 1024) -> None:
        self._store = store
        self._max_pending = max(1, int(max_pending))
        self._pending: deque[str] = deque()
        self._tasks: dict[str, ContextMaintenanceTask] = {}
        self._session_keys: dict[tuple[str, str], str] = {}

    def submit(self, *, tenant_id: str, session_id: str) -> ContextMaintenanceTask:
        key = (tenant_id, session_id)
        existing_id = self._session_keys.get(key)
        if existing_id:
            existing = self._tasks[existing_id]
            if existing.status in {"pending", "running"}:
                return existing
        if len(self._pending) >= self._max_pending:
            raise RuntimeError("context_maintenance_queue_full")
        now = time.time()
        task = ContextMaintenanceTask(
            task_id=f"ctxorg_{uuid.uuid4().hex[:20]}",
            tenant_id=tenant_id,
            session_id=session_id,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.task_id] = task
        self._session_keys[key] = task.task_id
        self._pending.append(task.task_id)
        return task

    def get(self, task_id: str) -> ContextMaintenanceTask | None:
        return self._tasks.get(task_id)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def run_pending(self, *, limit: int = 1) -> list[ContextMaintenanceTask]:
        completed: list[ContextMaintenanceTask] = []
        for _ in range(max(0, int(limit))):
            if not self._pending:
                break
            task_id = self._pending.popleft()
            task = replace(
                self._tasks[task_id],
                status="running",
                attempts=self._tasks[task_id].attempts + 1,
                updated_at=time.time(),
            )
            self._tasks[task_id] = task
            try:
                final = self._run(task)
            except Exception:  # noqa: BLE001 - background failure is observable only
                final = replace(
                    task,
                    status="failed",
                    error_code="snapshot_store_error",
                    updated_at=time.time(),
                )
            self._tasks[task_id] = final
            completed.append(final)
        return completed

    def _run(self, task: ContextMaintenanceTask) -> ContextMaintenanceTask:
        working = self._store.load(
            tenant_id=task.tenant_id, session_id=task.session_id
        )
        if working is None:
            return replace(
                task,
                status="failed",
                error_code="session_not_found",
                updated_at=time.time(),
            )
        plan = plan_context_maintenance(working)
        common = {
            "before_items": plan.before_items,
            "after_items": plan.after_items,
            "updated_at": time.time(),
        }
        if plan.patch is None:
            return replace(task, status="unchanged", **common)
        maintained = apply_patch(working, plan.patch)
        stored = self._store.compare_and_set(
            tenant_id=task.tenant_id,
            session_id=task.session_id,
            expected_version=working.version,
            value=maintained,
        )
        if not stored:
            return replace(
                task,
                status="conflicted",
                error_code="version_conflict",
                **common,
            )
        return replace(task, status="succeeded", **common)


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


__all__ = [
    "ContextMaintenancePlan",
    "ContextMaintenanceQueue",
    "ContextMaintenanceTask",
    "WorkingContextSnapshotStore",
    "plan_context_maintenance",
]
