"""Publish A2A invocation events, descriptors, and task snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

from a2a.types import Task

from ksadk.a2a.invocation_types import A2AInvocationOutputRef, A2AInvocationSnapshot
from ksadk.a2a.presentation import (
    ExecutionScopeDescriptor,
    ScopeOutputRef,
    validate_scope_transition,
)
from ksadk.events.adapters.a2a import A2AAdapterContext
from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemStarted,
    SourceRef,
)
from ksadk.events.content import ContentSnapshot, DataContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_part_id


class _InvocationPublicationMixin:
    async def _publish_one(self, state, event):
        persisted, _ = await self._event_store.persist_one(state.handle.session_id, event)
        state.cursor = max(state.cursor, persisted.seq)
        state.last_event_id = persisted.event_id
        if self._dispatcher is not None and state.handle.platform_task_id:
            await self._dispatcher.enqueue(
                platform_task_id=state.handle.platform_task_id,
                events=[persisted.model_dump(mode="json")],
            )
        return persisted

    async def _flush_pending(self, state):
        published = []
        while state.pending:
            published.append(await self._publish_one(state, state.pending[0]))
            state.pending.pop(0)
            await self._save_state(state)
        return published

    async def _publish_batch(self, state, events):
        if state.pending:
            await self._flush_pending(state)
        state.pending = list(events)
        # Persist a bounded delivery spool alongside the advanced adapter
        # state. Recovery drains it idempotently into the sole canonical log.
        await self._save_state(state)
        return await self._flush_pending(state)

    async def _publish(self, state, event):
        return (await self._publish_batch(state, [event]))[0]

    async def _descriptor_event(self, state, status, *, initial=False, cancel=None):
        values = state.descriptor.model_dump()
        values.update(
            status=status,
            output_refs=tuple(ScopeOutputRef(**ref.model_dump()) for ref in state.refs.values()),
        )
        if status in {"completed", "failed", "cancelled"}:
            values["ended_at"] = float(self._clock())
        if cancel is not None:
            values["cancel"] = cancel
        current = ExecutionScopeDescriptor.model_validate(values)
        if not initial:
            validate_scope_transition(state.descriptor, current)
        state.descriptor = current
        item = stable_item_id("a2a", state.handle.scope_id, "execution.scope")
        snapshot = ContentSnapshot(
            parts=(
                DataContent(
                    part_id=stable_part_id("a2a", item, "descriptor"),
                    data=current.model_dump(mode="json"),
                ),
            )
        )
        event_type = (
            "item.started"
            if initial
            else "item.completed"
            if status in {"completed", "failed", "cancelled"}
            else "item.snapshot_replaced"
        )
        common = dict(
            schema_version=2,
            seq=state.cursor + 1,
            timestamp=float(self._clock()),
            run_id=state.handle.run_id,
            scope_id=state.handle.scope_id,
            parent_scope_id=state.handle.parent_scope_id,
            source=SourceRef(framework="a2a", metadata={"schema": "execution.scope/v1"}),
            event_id=stable_event_id(
                "a2a",
                state.handle.scope_id,
                item,
                event_type,
                "descriptor",
                current.model_dump_json(),
                state.cursor,
            ),
            item_id=item,
            item_kind="status",
        )
        event = (
            ItemStarted(**common, initial=snapshot)
            if initial
            else ItemCompleted(**common, snapshot=snapshot)
            if event_type == "item.completed"
            else ItemSnapshotReplaced(**common, snapshot=snapshot)
        )
        return await self._publish(state, event)

    def _snapshot(self, state, status=None, error=None):
        selected = status or state.descriptor.status
        state.snapshot = A2AInvocationSnapshot(
            handle=state.handle,
            status=selected,
            source=state.source,
            observed_at_unix_seconds=float(self._clock()),
            event_cursor=state.cursor,
            last_event_id=state.last_event_id,
            output_refs=tuple(state.refs.values()),
            continuation_id=state.continuation_id
            if selected in {"input_required", "auth_required"}
            else None,
            error=error,
        )
        return state.snapshot

    async def _mapped(self, state, events):
        mapped = []
        for event in events:
            # A remote child never takes ownership of the public root run.
            if event.event_type.startswith("run."):
                continue
            if isinstance(event, ContinuationCreated):
                state.continuation_id = event.continuation_id
            if (
                isinstance(event, InteractionRequested)
                and event.interaction_kind in {"structured_input", "approval"}
            ):
                state.pending_input = event
            if (
                isinstance(event, InteractionResolved) and state.pending_input is not None
                and event.interaction_id == state.pending_input.interaction_id
            ):
                state.pending_input = None
            if isinstance(event, ContinuationResumed):
                state.pending_input = None
            if isinstance(event, ItemCompleted) and event.item_kind in {"message", "artifact"}:
                for part in event.snapshot.parts:
                    if getattr(part, "content_type", None) == "text":
                        ref = A2AInvocationOutputRef(
                            scope_id=state.handle.scope_id,
                            item_id=event.item_id,
                            part_id=part.part_id,
                        )
                        state.refs[(event.item_id, part.part_id)] = ref
            mapped.append(event)
        for persisted in await self._publish_batch(state, mapped):
            yield persisted

    async def _bind(self, state, payload, deadline):
        if isinstance(payload, Task):
            task_id, context_id = payload.id, payload.context_id
        else:
            task_id, context_id = (
                getattr(payload, "task_id", ""),
                getattr(payload, "context_id", ""),
            )
        if task_id:
            if state.handle.remote_task_id is None:
                await self._timed(
                    self._control_plane.bind_remote_task(
                        platform_task_id=state.handle.platform_task_id,
                        remote_task_id=task_id,
                        remote_context_id=context_id or None,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    ),
                    deadline,
                    "prepare",
                )
                state.handle = state.handle.bind_remote_task(task_id, context_id or None)
            elif (task_id, context_id or None) != (
                state.handle.remote_task_id,
                state.handle.remote_context_id,
            ):
                raise ValueError("A2A_REMOTE_BINDING_CONFLICT")
        if state.context is None:
            state.context = A2AAdapterContext(
                run_id=state.handle.run_id,
                context_id=context_id or state.handle.scope_id,
                task_id=task_id or None,
                fixed_scope_id=state.handle.scope_id,
                parent_scope_id=state.handle.parent_scope_id,
            )
        elif task_id:
            state.context.task_id, state.context.context_id = (
                task_id,
                context_id or state.context.context_id,
            )
