"""Local read-only shadow observer. Only primary events reach callers.

Comparison records structural equality, not semantic acceptance. Controls
invalidate the observation rather than duplicating approvals or mapping IDs
between engines. Reports are process-local; no durable shadow claim is made.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from uuid import uuid4

from ksadk.runtime import RuntimeAdapter


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass
class _Trace:
    event_ids: set = field(default_factory=set)
    kinds: Counter = field(default_factory=Counter)
    sequence: object = field(default_factory=hashlib.sha256)
    items: dict = field(default_factory=dict)
    output_ids: list = field(default_factory=list)
    status: str = "running"
    outcome: str | None = None
    terminal_count: int = 0
    invalid: bool = False

    def add(self, event):
        # Observation must not change primary delivery, including when a
        # legacy trace violates a newer canonical reducer's stricter ordering.
        try:
            if event.event_id in self.event_ids:
                return
            self.event_ids.add(event.event_id)
            self.kinds[event.event_type] += 1
            self.sequence.update(
                json.dumps(
                    [
                        event.event_type,
                        getattr(event, "item_kind", None),
                        getattr(event, "phase", None),
                        getattr(event, "status", None),
                    ]
                ).encode()
            )
            if event.event_type == "item.completed":
                self.items[event.item_id] = [
                    p.model_dump(mode="json", exclude={"part_id"}) for p in event.snapshot.parts
                ]
            if event.event_type == "run.interrupted":
                self.status = "interrupted"
            if event.event_type in {"run.completed", "run.failed", "run.canceled"}:
                self.terminal_count += 1
                self.status = event.status
                self.outcome = event.source.metadata.get("outcome") if event.source else None
                self.output_ids = [r.item_id for r in getattr(event, "output_refs", ())]
        except Exception:
            self.invalid = True

    def summary(self):
        missing = any(key not in self.items for key in self.output_ids)
        return {
            "status": self.status,
            "outcome": self.outcome,
            "terminal_count": self.terminal_count,
            "event_counts": dict(sorted(self.kinds.items())),
            "event_sequence_digest": self.sequence.hexdigest(),
            "result_digest": _digest([self.items[k] for k in self.output_ids if k in self.items]),
            "trace_complete": not self.invalid and not missing and self.terminal_count == 1,
        }


@dataclass
class _Pair:
    primary: object
    shadow: object = None
    task: asyncio.Task | None = None
    status: str = "running"
    primary_trace: _Trace = field(default_factory=_Trace)
    shadow_trace: _Trace = field(default_factory=_Trace)


class ReadOnlyShadowRuntimeAdapter(RuntimeAdapter):
    def __init__(self, primary, shadow, *, input_digest: str, timeout_seconds=120):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600:
            raise ValueError("native_shadow_timeout_invalid")
        super().__init__(primary.runtime)
        self._primary, self._shadow = primary, shadow
        self._input_digest, self._timeout = input_digest, timeout_seconds
        self._pairs: dict[str, _Pair] = {}
        self._closed = False
        self._namespace = uuid4().hex

    async def preflight(self):
        await self._primary.preflight()
        await self._shadow.preflight()

    def capabilities(self):
        return self._primary.capabilities()

    def is_handle_attached(self, handle):
        return self._primary.is_handle_attached(handle)

    async def start(self, request):
        if self._closed:
            raise ValueError("native_shadow_closed")
        # Copy before primary admission can modify a caller-owned request.
        secondary = request.model_copy(deep=True)
        secondary.session_id = "shadow-" + _digest([self._namespace, request.session_id])
        # Primary transport IDs must never name a shadow execution.
        secondary.metadata.pop("invocation_id", None)
        handle = await self._primary.start(request)
        pair = _Pair(primary=handle.model_copy(deep=True))
        self._pairs[handle.run_id] = pair
        pair.task = asyncio.create_task(self._observe(pair, secondary))
        return handle

    def _pair(self, handle):
        pair = self._pairs.get(handle.run_id)
        if pair is not None and pair.primary != handle:
            raise ValueError("native_shadow_handle_changed")
        return pair

    async def _observe(self, pair, request):
        try:
            async with asyncio.timeout(self._timeout):
                pair.shadow = await self._shadow.start(request)
                async for event in self._shadow.stream(pair.shadow):
                    pair.shadow_trace.add(event)
                pair.status = "complete" if pair.shadow_trace.terminal_count == 1 else "incomplete"
        except TimeoutError:
            pair.status = "timeout"
        except asyncio.CancelledError:
            if pair.status == "running":
                pair.status = "canceled"
        except Exception:
            pair.status = "error"  # Provider errors may contain private data.
        finally:
            if pair.shadow is not None:
                try:
                    try:
                        await self._shadow.cancel(pair.shadow)
                    finally:
                        await self._shadow.close(pair.shadow)
                except Exception:
                    pair.status = "cleanup_error"

    async def stream(self, handle):
        pair = self._pair(handle)
        async for event in self._primary.stream(handle):
            if pair is not None:
                pair.primary_trace.add(event)
            yield event

    async def shadow_comparison(self, handle):
        pair = self._pair(handle)
        if pair is None:
            return {"schema_version": "native-shadow/v1", "status": "unavailable"}
        if pair.task is not None:
            await asyncio.shield(asyncio.gather(pair.task, return_exceptions=True))
        primary, shadow = pair.primary_trace.summary(), pair.shadow_trace.summary()
        complete = (
            pair.status == "complete" and primary["trace_complete"] and shadow["trace_complete"]
        )
        return {
            "schema_version": "native-shadow/v1",
            "mode": "read_only_snapshot",
            "status": "complete"
            if complete
            else ("primary_incomplete" if pair.status == "complete" else pair.status),
            "primary_run_id": handle.run_id,
            "shadow_run_id": pair.shadow.run_id if pair.shadow else None,
            "input_digest": self._input_digest,
            "primary": primary,
            "shadow": shadow,
            "equal": {k: primary[k] == shadow[k] for k in primary} if complete else None,
            "semantic_acceptance": "not_evaluated",
        }

    async def _invalidate(self, handle):
        pair = self._pair(handle)
        if pair is not None:
            pair.status = "control_intervened"
            if pair.task is not None and not pair.task.done():
                pair.task.cancel()
                await asyncio.gather(pair.task, return_exceptions=True)

    async def cancel(self, handle):
        # Primary control is never held up by a shadow model request.
        result = await self._primary.cancel(handle)
        await self._invalidate(handle)
        return result

    async def pause(self, handle):
        result = await self._primary.pause(handle)
        await self._invalidate(handle)
        return result

    async def resume(self, handle, target, payload):
        await self._invalidate(handle)
        return await self._primary.resume(handle, target, payload)

    async def submit(self, handle, payload):
        await self._invalidate(handle)
        return await self._primary.submit(handle, payload)

    async def steer(self, handle, payload):
        await self._invalidate(handle)
        return await self._primary.steer(handle, payload)

    async def inject(self, handle, payload):
        await self._invalidate(handle)
        return await self._primary.inject(handle, payload)

    async def attach(self, handle):
        return await self._primary.attach(handle)

    async def durable_restore(self, handle):
        return await self._primary.durable_restore(handle)

    async def checkpoint(self, handle):
        return await self._primary.checkpoint(handle)

    async def close(self, handle):
        pair = self._pair(handle)
        if pair is not None and pair.task is not None and not pair.task.done():
            await self._invalidate(handle)
        await self._primary.close(handle)

    async def close_all(self):
        if self._closed:
            return
        self._closed = True
        try:
            for pair in self._pairs.values():
                await self.close(pair.primary)
        finally:
            await self._shadow.close_all()
            close = getattr(self._primary, "close_all", None)
            if close is not None:
                await close()
