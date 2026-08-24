"""Validated canonical event ingestion and deterministic conformance recovery."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ksadk.events.canonical import (
    ErrorInfo,
    ItemCompleted,
    ItemFailed,
    ItemKind,
    ItemSnapshotReplaced,
    RunFailed,
    RuntimeEvent,
    SourceRef,
    dump_runtime_event,
)
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.identity import stable_event_id
from ksadk.events.reducer import StreamConformanceError, StreamReducer
from ksadk.kernel.contracts import ActivationWriteGuard, WriteContext

Publisher = Callable[[str, RuntimeEvent], Awaitable[None]]


def _reconciliation_reason(event: RuntimeEvent) -> str:
    if isinstance(event, ItemCompleted):
        return "completed_snapshot_mismatch"
    if isinstance(event, ItemSnapshotReplaced):
        return "authoritative_snapshot_replace"
    raise RuntimeError(f"reconciled patch has no declared metric semantics: {event.event_type!r}")


class PipelineMetrics:
    """Small metrics seam; production collectors can mirror ``increment``."""

    def __init__(self) -> None:
        self._counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()

    def increment(self, name: str, **labels: str) -> None:
        self._counts[(name, tuple(sorted(labels.items())))] += 1

    def value(self, name: str, **labels: str) -> int:
        return self._counts[(name, tuple(sorted(labels.items())))]


class CanonicalEventPipeline:
    """Prevalidate, persist, reduce and publish one canonical source mutation."""

    def __init__(
        self,
        store: RuntimeEventStore,
        *,
        session_id: str,
        reducer: StreamReducer | None = None,
        publisher: Publisher | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must be nonempty")
        self.store = store
        self.session_id = session_id
        self.reducer = reducer or StreamReducer()
        self.publisher = publisher
        self.metrics = metrics or PipelineMetrics()
        self._ingest_lock = asyncio.Lock()
        self._hydrated_run_id = self.reducer.snapshot().run_id

    async def ingest(self, event: RuntimeEvent) -> tuple[RuntimeEvent, ...]:
        """Ingest one fact; invalid source facts become durable failure facts."""

        async with self._ingest_lock:
            return await self._ingest_locked(event)

    async def emit(
        self, event: RuntimeEvent, *, write_context: WriteContext
    ) -> RuntimeEvent:
        """Fenced typed append: persist (guard CAS) before publish.

        与 :meth:`ingest` 的差异：``emit`` 是 owner 内部写入路径，要求
        ``WriteContext(activation_id, fencing_token)``，通过 typed
        ``RuntimeEventStore``（SessionEventStore envelope 视图）走
        ``append(event, guard=write_context)``；Store 在事务内比较 fence 后
        才分配 seq，旧 owner 在 takeover 后写入会得到
        :class:`~ksadk.kernel.errors.StaleFenceError`。WriteContext 只作为
        写权限 guard，不进入 RuntimeEvent payload，也不进入公网 projection。
        """

        if not isinstance(write_context, ActivationWriteGuard):
            raise TypeError(
                "emit requires a typed WriteContext(activation_id, fencing_token)"
            )
        if self.store.event_store is None:
            raise RuntimeError(
                "emit requires a SessionEventStore-backed typed RuntimeEventStore"
            )
        if getattr(self.store, "session_id", None) != self.session_id:
            raise ValueError("typed RuntimeEventStore session does not match pipeline")
        # 先在影子 reducer 上预检 conformance，非法事实绝不落库。
        shadow = copy.deepcopy(self.reducer)
        shadow.apply(event.model_copy(update={"seq": self._validation_seq(event)}))
        persisted = await self.store.append(event, guard=write_context)
        last_seq = self.reducer.snapshot().last_seq
        if last_seq is None or persisted.seq > last_seq:
            self.reducer.apply(persisted)
        await self._publish(persisted)
        return persisted

    async def _ingest_locked(self, event: RuntimeEvent) -> tuple[RuntimeEvent, ...]:
        await self._hydrate_run_if_needed(event.run_id)
        existing = await self.store.resolve_existing(self.session_id, event)
        if existing is not None:
            last_seq = self.reducer.snapshot().last_seq
            if last_seq is None or existing.seq > last_seq:
                self.reducer.apply(existing)
            await self._publish(existing)
            return (existing,)

        candidate = event.model_copy(update={"seq": self._validation_seq(event)})
        shadow = copy.deepcopy(self.reducer)
        try:
            preview = shadow.apply(candidate)
        except StreamConformanceError as error:
            return await self._recover(event, error)
        reconciliation_reason = _reconciliation_reason(candidate) if preview.reconciled else None

        persisted, created = await self.store.persist_one(self.session_id, event)
        self.reducer.apply(persisted)
        if created and reconciliation_reason is not None:
            self.metrics.increment(
                "stream_projection_reconciled_total",
                source=event.source.framework,
                reason=reconciliation_reason,
            )
        await self._publish(persisted)
        return (persisted,)

    async def _hydrate_run_if_needed(self, run_id: str) -> None:
        if self._hydrated_run_id == run_id:
            return
        snapshot = self.reducer.snapshot()
        if snapshot.run_id is not None and snapshot.run_id != run_id:
            # Let the reducer produce its normal structured run-id error.
            return
        for persisted in await self.store.list(self.session_id, run_id=run_id):
            self.reducer.apply(persisted)
        self._hydrated_run_id = run_id

    def _validation_seq(self, event: RuntimeEvent) -> int:
        last_seq = self.reducer.snapshot().last_seq
        return max((last_seq or 0) + 1, event.seq)

    async def _recover(
        self, offending: RuntimeEvent, error: StreamConformanceError
    ) -> tuple[RuntimeEvent, ...]:
        fingerprint = _canonical_fingerprint(offending)
        terminal = await self.store.event_by_id(
            self.session_id,
            self._recovery_terminal_event_id(offending),
        )
        owner_locator = await self.store.event_by_id(
            self.session_id,
            self._recovery_owner_event_id(offending),
        )
        if terminal is not None or owner_locator is not None:
            plan = self._load_recovery_plan(
                offending,
                fingerprint,
                terminal=terminal,
                owner_locator=owner_locator,
            )
        else:
            plan = self._new_recovery_plan(offending, error, fingerprint)
            self.metrics.increment(
                "stream_conformance_error_total",
                source=error.source,
                reason=error.code,
            )

        planned = self._recovery_facts(offending, plan, fingerprint)
        persisted_group: list[RuntimeEvent] = []
        # Persist the complete plan before changing live projection or emitting.
        for fact in planned:
            persisted, _created = await self.store.persist_one(self.session_id, fact)
            persisted_group.append(persisted)
        # Apply the complete durable group before the first publish attempt.
        for persisted in persisted_group:
            last_seq = self.reducer.snapshot().last_seq
            if last_seq is None or persisted.seq > last_seq:
                self.reducer.apply(persisted)
        # A retry republishes the whole group from its first member.  Duplicate
        # event ids are allowed at this live boundary; missing facts are not.
        for persisted in persisted_group:
            await self._publish(persisted)
        return tuple(persisted_group)

    def _load_recovery_plan(
        self,
        offending: RuntimeEvent,
        fingerprint: str,
        *,
        terminal: RuntimeEvent | None,
        owner_locator: RuntimeEvent | None,
    ) -> dict[str, Any]:
        terminal_id = self._recovery_terminal_event_id(offending)
        owner_locator_id = self._recovery_owner_event_id(offending)
        existing = tuple(event for event in (terminal, owner_locator) if event is not None)
        owner_ids: set[str] = set()
        for persisted in existing:
            metadata = persisted.source.metadata
            if (
                metadata.get("recovery_for_event_id") != offending.event_id
                or metadata.get("offending_fingerprint") != fingerprint
            ):
                raise ValueError(f"RuntimeEvent recovery collision for {offending.event_id!r}")
            owner_id = metadata.get("recovery_plan_owner_event_id")
            if not isinstance(owner_id, str) or not owner_id:
                raise ValueError("persisted recovery is missing its plan owner ref")
            owner_ids.add(owner_id)
        if len(owner_ids) != 1:
            raise ValueError(f"RuntimeEvent recovery collision for {offending.event_id!r}")

        owner_id = owner_ids.pop()
        if owner_id == terminal_id:
            owner = terminal
        elif owner_id == owner_locator_id:
            owner = owner_locator
        else:
            raise ValueError(f"RuntimeEvent recovery collision for {offending.event_id!r}")
        if owner is None:
            raise ValueError("persisted recovery plan owner is missing")
        if terminal is not None and (
            terminal.event_id != terminal_id or terminal.event_type != "run.failed"
        ):
            raise ValueError(f"RuntimeEvent recovery collision for {offending.event_id!r}")
        if owner_locator is not None and (
            owner_locator.event_id != owner_locator_id or owner_locator.event_type != "item.failed"
        ):
            raise ValueError(f"RuntimeEvent recovery collision for {offending.event_id!r}")
        owner_metadata = owner.source.metadata
        if (
            owner.event_id != owner_id
            or owner_metadata.get("recovery_for_event_id") != offending.event_id
            or owner_metadata.get("offending_fingerprint") != fingerprint
            or owner_metadata.get("recovery_plan_owner_event_id") != owner_id
        ):
            raise ValueError(f"RuntimeEvent recovery collision for {offending.event_id!r}")
        plan_value = owner_metadata.get("recovery_plan")
        if not isinstance(plan_value, dict):
            raise ValueError("persisted recovery plan owner is missing its complete plan")
        if (
            plan_value.get("offending_fingerprint") != fingerprint
            or plan_value.get("owner_event_id") != owner_id
        ):
            raise ValueError(f"RuntimeEvent recovery collision for {offending.event_id!r}")
        entries = plan_value.get("events")
        if (
            not isinstance(entries, list)
            or not entries
            or not isinstance(entries[0], dict)
            or entries[0].get("event_id") != owner_id
            or entries[0].get("event_type")
            != ("run.failed" if owner_id == terminal_id else "item.failed")
            or not isinstance(entries[-1], dict)
            or entries[-1].get("event_id") != terminal_id
            or entries[-1].get("event_type") != "run.failed"
        ):
            raise ValueError(f"RuntimeEvent recovery collision for {offending.event_id!r}")
        return plan_value

    def _new_recovery_plan(
        self,
        offending: RuntimeEvent,
        error: StreamConformanceError,
        fingerprint: str,
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        open_items = sorted(
            (item for item in self.reducer.snapshot().items if item.status == "open"),
            key=lambda item: (item.scope_id, item.item_id),
        )
        for index, item in enumerate(open_items):
            entries.append(
                {
                    "event_id": (
                        self._recovery_owner_event_id(offending)
                        if index == 0
                        else stable_event_id(
                            "ksadk",
                            item.scope_id,
                            item.item_id,
                            "item.failed",
                            "recovery",
                            offending.event_id,
                            0,
                        )
                    ),
                    "event_type": "item.failed",
                    "scope_id": item.scope_id,
                    "item_id": item.item_id,
                    "item_kind": item.item_kind,
                }
            )
        entries.append(
            {
                "event_id": self._recovery_terminal_event_id(offending),
                "event_type": "run.failed",
                "scope_id": offending.scope_id,
            }
        )
        owner_event_id = str(entries[0]["event_id"])
        return {
            "version": 1,
            "owner_event_id": owner_event_id,
            "offending_fingerprint": fingerprint,
            "error": {
                "code": error.code,
                "source": error.source,
                "scope_id": error.scope_id,
                "item_id": error.item_id,
            },
            "events": entries,
        }

    @staticmethod
    def _recovery_owner_event_id(offending: RuntimeEvent) -> str:
        return stable_event_id(
            "ksadk",
            "recovery",
            offending.event_id,
            "item.failed",
            "plan-owner",
            offending.event_id,
            0,
        )

    @staticmethod
    def _recovery_terminal_event_id(offending: RuntimeEvent) -> str:
        # The session-scoped offending event id is the collision domain.  Do
        # not include mutable candidate facts such as run/scope here: a retry
        # that reuses event_id with different facts must hit this tombstone and
        # fail fingerprint validation before writing a second recovery group.
        return stable_event_id(
            "ksadk",
            "recovery",
            offending.event_id,
            "run.failed",
            "tombstone",
            offending.event_id,
            0,
        )

    @staticmethod
    def _recovery_facts(
        offending: RuntimeEvent,
        plan: dict[str, Any],
        fingerprint: str,
    ) -> tuple[RuntimeEvent, ...]:
        error_payload = plan.get("error")
        entries = plan.get("events")
        owner_event_id = plan.get("owner_event_id")
        if (
            not isinstance(error_payload, dict)
            or not isinstance(entries, list)
            or not isinstance(owner_event_id, str)
            or not owner_event_id
        ):
            raise ValueError("persisted recovery plan is malformed")
        error_info = ErrorInfo(
            code=str(error_payload.get("code") or "stream_conformance_error"),
            message="Canonical stream conformance failure",
            source=str(error_payload.get("source") or offending.source.framework),
            scope_id=str(error_payload.get("scope_id") or offending.scope_id),
            item_id=(str(error_payload["item_id"]) if error_payload.get("item_id") else None),
            source_ref=offending.source,
        )
        facts: list[RuntimeEvent] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("persisted recovery plan event is malformed")
            event_id = str(entry.get("event_id") or "")
            metadata: dict[str, Any] = {
                "recovery_for_event_id": offending.event_id,
                "offending_fingerprint": fingerprint,
                "recovery_plan_owner_event_id": owner_event_id,
            }
            if event_id == owner_event_id:
                metadata["recovery_plan"] = plan
            recovery_source = SourceRef(
                framework="ksadk",
                native_event_id=offending.event_id,
                native_run_id=offending.run_id,
                metadata=metadata,
            )
            if entry.get("event_type") == "item.failed":
                scope_id = str(entry.get("scope_id") or "")
                item_id = str(entry.get("item_id") or "")
                facts.append(
                    ItemFailed(
                        schema_version=2,
                        event_id=event_id,
                        seq=0,
                        timestamp=offending.timestamp,
                        run_id=offending.run_id,
                        run_seq=offending.run_seq,
                        scope_id=scope_id,
                        source=recovery_source,
                        item_id=item_id,
                        item_kind=cast(ItemKind, entry.get("item_kind") or "message"),
                        error=error_info.model_copy(
                            update={"scope_id": scope_id, "item_id": item_id}
                        ),
                    )
                )
            elif entry.get("event_type") == "run.failed":
                facts.append(
                    RunFailed(
                        schema_version=2,
                        event_id=event_id,
                        seq=0,
                        timestamp=offending.timestamp,
                        run_id=offending.run_id,
                        run_seq=offending.run_seq,
                        scope_id=str(entry.get("scope_id") or offending.scope_id),
                        parent_scope_id=offending.parent_scope_id,
                        source=recovery_source,
                        status="failed",
                        error=error_info,
                    )
                )
            else:
                raise ValueError("persisted recovery plan has unsupported event type")
        return tuple(facts)

    async def _publish(self, event: RuntimeEvent) -> None:
        if self.publisher is not None:
            await self.publisher(self.session_id, event)


__all__ = ["CanonicalEventPipeline", "PipelineMetrics"]


def _canonical_fingerprint(event: RuntimeEvent) -> str:
    payload = dump_runtime_event(event)
    payload.pop("seq", None)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
