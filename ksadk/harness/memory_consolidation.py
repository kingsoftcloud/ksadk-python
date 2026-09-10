"""Best-effort background consolidation for long-term Memory.

The Harness owns the deterministic consolidation policy, while deployment code owns
the worker/process.  Submitting a task never performs provider I/O, so a slow or
unavailable Memory Service cannot block the Agent Loop.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Literal

from ksadk.memory.models import MemoryRecord, MemoryScope, MemorySearchRequest

ConsolidationStatus = Literal["pending", "running", "succeeded", "failed"]


@dataclass(frozen=True)
class MemoryConsolidationTask:
    task_id: str
    scope: MemoryScope
    scope_id: str
    status: ConsolidationStatus = "pending"
    attempts: int = 0
    scanned: int = 0
    superseded: int = 0
    error_code: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


class MemoryConsolidationQueue:
    """Bounded, observable task queue; safe to replace with a cloud task adapter."""

    def __init__(self, provider, *, max_pending: int = 1024) -> None:
        self._provider = provider
        self._max_pending = max(1, int(max_pending))
        self._pending: deque[str] = deque()
        self._tasks: dict[str, MemoryConsolidationTask] = {}
        self._scope_keys: dict[tuple[str, str], str] = {}

    def submit(self, *, scope: MemoryScope, scope_id: str) -> MemoryConsolidationTask:
        """Coalesce duplicate requests for the same scope without provider I/O."""
        key = (scope, scope_id)
        existing_id = self._scope_keys.get(key)
        if existing_id:
            existing = self._tasks[existing_id]
            if existing.status in {"pending", "running"}:
                return existing
        if len(self._pending) >= self._max_pending:
            raise RuntimeError("memory_consolidation_queue_full")
        now = time.time()
        task = MemoryConsolidationTask(
            task_id=f"memorg_{uuid.uuid4().hex[:20]}",
            scope=scope,
            scope_id=scope_id,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.task_id] = task
        self._scope_keys[key] = task.task_id
        self._pending.append(task.task_id)
        return task

    def get(self, task_id: str) -> MemoryConsolidationTask | None:
        return self._tasks.get(task_id)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def run_pending(self, *, limit: int = 1) -> list[MemoryConsolidationTask]:
        """Run a bounded worker batch; failures are captured, never re-raised."""
        completed: list[MemoryConsolidationTask] = []
        for _ in range(max(0, int(limit))):
            if not self._pending:
                break
            task_id = self._pending.popleft()
            task = self._tasks[task_id]
            running = replace(
                task,
                status="running",
                attempts=task.attempts + 1,
                updated_at=time.time(),
            )
            self._tasks[task_id] = running
            try:
                scanned, superseded = self._consolidate(running)
                final = replace(
                    running,
                    status="succeeded",
                    scanned=scanned,
                    superseded=superseded,
                    updated_at=time.time(),
                )
            except Exception:  # noqa: BLE001 - worker failure is observable degradation
                final = replace(
                    running,
                    status="failed",
                    error_code="provider_error",
                    updated_at=time.time(),
                )
            self._tasks[task_id] = final
            completed.append(final)
        return completed

    def _consolidate(self, task: MemoryConsolidationTask) -> tuple[int, int]:
        result = self._provider.search(
            MemorySearchRequest(
                query="",
                scopes=[(task.scope, task.scope_id)],
                memory_types=["profile", "fact", "episode"],
                top_k=512,
                max_tokens=1_000_000,
                min_score=0.0,
            )
        )
        if result.status != "ok":
            raise RuntimeError(result.error_code or "provider_error")
        records = list(result.records)
        winners: dict[tuple[str, str], MemoryRecord] = {}
        losers: list[MemoryRecord] = []
        for record in records:
            slot = str(record.metadata.get("slot_key") or "")
            # No explicit slot means no deterministic merge authority.
            if not slot or record.write_policy == "locked":
                continue
            key = (record.memory_type, slot)
            current = winners.get(key)
            if current is None:
                winners[key] = record
                continue
            winner, loser = max(
                (current, record), key=lambda item: (item.version, item.updated_at)
            ), min((current, record), key=lambda item: (item.version, item.updated_at))
            winners[key] = winner
            losers.append(loser)
        for loser in {record.memory_id: record for record in losers}.values():
            winner = winners[(loser.memory_type, str(loser.metadata["slot_key"]))]
            superseded = replace(
                loser,
                status="superseded",
                valid_to=winner.updated_at or winner.created_at,
                metadata={
                    **loser.metadata,
                    "superseded_by": winner.memory_id,
                    "superseded_reason": "background_consolidation",
                },
            )
            self._provider.upsert(superseded, expected_version=loser.version)
        return len(records), len({record.memory_id for record in losers})


__all__ = ["MemoryConsolidationQueue", "MemoryConsolidationTask"]
