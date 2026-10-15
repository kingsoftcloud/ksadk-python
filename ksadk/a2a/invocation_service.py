"""Managed A2A invocation lifecycle over the canonical store and Space wire port.

The canonical store allocates sequence numbers. The service keeps transient
protocol state only; it never creates another event log or stores DI objects.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone

from a2a.types import Message, StreamResponse, Task, TaskState, TaskStatusUpdateEvent

from ksadk.a2a._invocation_state import (
    A2AInvocationEventStore as A2AInvocationEventStore,
)
from ksadk.a2a._invocation_state import (
    A2AInvocationTimeouts as A2AInvocationTimeouts,
)
from ksadk.a2a._invocation_state import (
    _CancelDelivery,
    _ServiceCheckpoint,
    _State,
)
from ksadk.a2a.invocation_policy import A2AApprovalRequired, A2AInvocationPolicy
from ksadk.a2a.invocation_types import (
    A2ACancelResult,
    A2AContinuationRequest,
    A2AInvocationError,
    A2AInvocationOutputRef,
    A2AInvocationRequest,
    A2AInvocationSnapshot,
)
from ksadk.a2a.message_digest import canonical_message_sha256
from ksadk.a2a.presentation import (
    ExecutionScopeDescriptor,
    PublicAgentRef,
    ScopeCancelState,
    ScopeOutputRef,
    ScopeTriggerRef,
    validate_scope_transition,
)
from ksadk.events.adapters.a2a import A2AAdapterContext, A2AEventAdapter
from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemFailed,
    ItemSnapshotReplaced,
    ItemStarted,
    SourceRef,
    parse_runtime_event,
)
from ksadk.events.content import ContentSnapshot, DataContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_part_id

from ksadk.a2a.invocation_publication import _InvocationPublicationMixin

logger = logging.getLogger(__name__)


class A2AInvocationService(_InvocationPublicationMixin):
    def __init__(
        self,
        *,
        space_client,
        event_store: A2AInvocationEventStore,
        policy: A2AInvocationPolicy,
        timeouts=None,
        clock=None,
        event_dispatcher=None,
        trigger_resolver=None,
        checkpoint_store=None,
        recover_checkpoint_leases=False,
    ):
        self._space_client = space_client
        self._control_plane = space_client.control_plane
        self._event_store, self._policy = event_store, policy
        self._timeouts = timeouts or A2AInvocationTimeouts()
        self._clock = clock or time.time
        self._dispatcher = event_dispatcher
        self._trigger_resolver = trigger_resolver
        self._states: dict[str, _State] = {}
        self._recover_checkpoint_leases = recover_checkpoint_leases
        self._checkpoint_store = checkpoint_store
        self._checkpoint_leases = {}

    @property
    def durable_recovery_available(self) -> bool:
        return self._checkpoint_store is not None

    async def _save_state(self, state):
        lease = self._checkpoint_leases.get(state.handle.scope_id)
        if lease is None:
            return
        from ksadk.a2a.invocation_checkpoint import A2AInvocationCheckpoint

        recovery = _ServiceCheckpoint(
            descriptor=state.descriptor,
            adapter=state.adapter.checkpoint(state.context) if state.context else None,
            snapshot=state.snapshot,
            tokens=state.tokens,
            refs=list(state.refs.values()),
            continuation_id=state.continuation_id,
            pending_continuation=state.pending_continuation,
            pending_input=state.pending_input,
            source=state.source,
            cursor=state.cursor,
            last_event_id=state.last_event_id,
            pending=[event.model_dump(mode="json") for event in state.pending],
        )
        await lease.save(
            A2AInvocationCheckpoint(
                request=state.request,
                handle=state.handle,
                send_entered=state.send_entered,
                state=recovery.model_dump(mode="json", by_alias=True),
            )
        )

    def _restore_state(self, checkpoint):
        recovery = _ServiceCheckpoint.model_validate(checkpoint.state)
        state = _State(
            request=checkpoint.request,
            handle=checkpoint.handle,
            descriptor=recovery.descriptor,
            snapshot=recovery.snapshot,
            send_entered=checkpoint.send_entered,
            tokens=recovery.tokens,
            refs={(ref.item_id, ref.part_id): ref for ref in recovery.refs},
            continuation_id=recovery.continuation_id,
            pending_continuation=recovery.pending_continuation,
            pending_input=recovery.pending_input,
            source=recovery.source,
            cursor=recovery.cursor,
            last_event_id=recovery.last_event_id,
            pending=[parse_runtime_event(event) for event in recovery.pending],
        )
        if recovery.adapter is not None:
            state.adapter, state.context = A2AEventAdapter.restore_checkpoint(recovery.adapter)
        if (
            state.descriptor.scope_id != state.handle.scope_id
            or state.descriptor.parent_scope_id != state.handle.parent_scope_id
            or state.descriptor.binding_id != state.request.spec.binding_id
            or state.descriptor.agent.id != state.request.spec.target_agent_id
            or state.descriptor.agent.version_id != state.request.spec.expected_version_id
            or state.descriptor.trigger_ref.call_id != state.handle.call_id
            or any(ref.scope_id != state.handle.scope_id for ref in state.refs.values())
            or (
                state.context is not None
                and (
                    state.context.scope_id != state.handle.scope_id
                    or state.context.run_id != state.handle.run_id
                )
            )
            or any(
                event.scope_id != state.handle.scope_id or event.run_id != state.handle.run_id
                for event in state.pending
            )
        ):
            raise ValueError("A2A_CHECKPOINT_IDENTITY_CONFLICT")
        if (
            state.descriptor.status in {"completed", "failed", "cancelled"}
            and state.snapshot is None
        ):
            self._snapshot(state)
        return state

    @asynccontextmanager
    async def _operation(self, handle):
        if self._checkpoint_store is None:
            yield
            return
        options = {"wait_for_recovery": True} if self._recover_checkpoint_leases else {}
        async with self._checkpoint_store.lease(handle.scope_id, **options) as lease:
            self._checkpoint_leases[handle.scope_id] = lease
            ready = False
            try:
                saved = await lease.load()
                if saved is not None:
                    self._states[handle.scope_id] = self._restore_state(saved)
                    state = self._state(handle)
                    await self._flush_pending(state)
                else:
                    self._states.pop(handle.scope_id, None)
                ready = True
                yield
            finally:
                try:
                    state = self._states.get(handle.scope_id)
                    if ready and state is not None:
                        await self._save_state(state)
                finally:
                    self._checkpoint_leases.pop(handle.scope_id, None)

    @classmethod
    def from_space_client(cls, space_client, **kwargs):
        kwargs.setdefault("event_dispatcher", space_client.event_dispatcher)
        return cls(space_client=space_client, **kwargs)

    def handle(self, scope_id):
        return self._states[scope_id].handle

    async def _terminal_replay(self, state):
        """Restore the caller's result from existing facts, without another send."""
        events = [
            event
            for event in await self._event_store.list(
                state.handle.session_id, run_id=state.handle.run_id
            )
            if event.run_id == state.handle.run_id and event.scope_id == state.handle.scope_id
        ]
        completed = {event.item_id: event for event in events if isinstance(event, ItemCompleted)}
        descriptor_item = stable_item_id("a2a", state.handle.scope_id, "execution.scope")
        descriptor = completed.get(descriptor_item)
        if descriptor is None or not any(
            isinstance(part, DataContent)
            and isinstance(part.data, dict)
            and ExecutionScopeDescriptor.model_validate(part.data) == state.descriptor
            for part in descriptor.snapshot.parts
        ):
            raise ValueError("A2A_TERMINAL_RECOVERY_FACT_REQUIRED")
        for ref in state.refs.values():
            item = completed.get(ref.item_id)
            if item is None or (
                ref.part_id is not None
                and not any(part.part_id == ref.part_id for part in item.snapshot.parts)
            ):
                raise ValueError("A2A_TERMINAL_RECOVERY_FACT_REQUIRED")
        return events

    async def original_request(self, handle):
        """Recover the exact initial request; never refresh its deadline or token.

        The same lease and identity checks as invoke/get protect the lookup.
        None means no durable/local invocation exists, not an unknown send.
        """
        async with self._operation(handle):
            if handle.scope_id not in self._states:
                return None
            return self._state(handle).request

    async def pending_continuation(self, handle):
        """Read the immutable supplemental request persisted before approval."""
        async with self._operation(handle):
            if handle.scope_id not in self._states:
                return None
            state = self._state(handle)
            if state.snapshot is not None and state.snapshot.terminal:
                return None
            return state.pending_continuation

    async def pending_input_request(self, handle):
        """Recover the exact persisted prompt, without another remote send."""
        async with self._operation(handle):
            state = self._state(handle)
            if state.snapshot is None or state.snapshot.status not in {
                "input_required",
                "auth_required",
            }:
                return None
            event = state.pending_input
            if event is None or event.run_id != handle.run_id or event.scope_id != handle.scope_id:
                raise ValueError("A2A_INPUT_RECOVERY_FACT_REQUIRED")
            return event

    def snapshot(self, handle):
        """Return the latest local snapshot without performing network I/O."""
        return self._state(handle).snapshot

    async def replay(self, handle):
        return tuple(
            event
            for event in await self._event_store.list(handle.session_id, run_id=handle.run_id)
            if event.scope_id == handle.scope_id
        )

    async def _timed(self, awaitable, deadline, segment):
        remaining = deadline - self._clock()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError("A2A_OVERALL_TIMEOUT")
        limit = getattr(self._timeouts, segment + "_seconds")
        try:
            return await asyncio.wait_for(awaitable, min(remaining, limit))
        except asyncio.TimeoutError as exc:
            code = "A2A_OVERALL_TIMEOUT" if remaining <= limit else f"A2A_{segment.upper()}_TIMEOUT"
            raise TimeoutError(code) from exc

    async def _authorize(self, request, route=None, *, deadline=None):
        decision = await self._timed(
            self._policy.evaluate(request, route_kind=route),
            request.deadline_unix_seconds if deadline is None else deadline,
            "prepare",
        )
        if decision.action == "approval_required":
            raise A2AApprovalRequired(decision.reason_code or "A2A_APPROVAL_REQUIRED")
        if decision.action != "allow":
            raise PermissionError(decision.reason_code or "A2A_APPROVAL_REQUIRED")

    @staticmethod
    def _validate_prepared(request, prepared):
        prepared.target.assert_matches(
            target_agent_id=request.spec.target_agent_id,
            expected_version_id=request.spec.expected_version_id,
        )
        if prepared.route.kind != "hosted_gateway":
            raise PermissionError("A2A_HOSTED_ROUTE_REQUIRED")

    async def invoke(
        self, request: A2AInvocationRequest, *, trigger_ref: ScopeTriggerRef | None = None
    ):
        async with self._operation(request.handle):
            async for event in self._invoke(request, trigger_ref=trigger_ref):
                yield event

    async def _invoke(
        self, request: A2AInvocationRequest, *, trigger_ref: ScopeTriggerRef | None = None
    ):
        if request.spec.expected_version_id is None:
            raise ValueError("A2A_VERSION_PIN_REQUIRED")
        trigger = trigger_ref or (
            self._trigger_resolver(request) if self._trigger_resolver else None
        )
        if not isinstance(trigger, ScopeTriggerRef):
            raise ValueError("A2A_TRIGGER_REF_REQUIRED")
        if (
            trigger.scope_id != request.handle.parent_scope_id
            or trigger.call_id != request.handle.call_id
        ):
            raise ValueError("A2A_TRIGGER_REF_CONFLICT")
        previous = self._states.get(request.scope_id)
        if previous:
            if previous.request != request or previous.descriptor.trigger_ref != trigger:
                raise ValueError("A2A_INVOCATION_IDENTITY_CONFLICT")
            if previous.snapshot and previous.snapshot.terminal:
                for event in await self._terminal_replay(previous):
                    yield event
                return
            if previous.send_entered or previous.handle.remote_task_id is not None:
                # A durable possible-send receipt forbids automatic re-send.
                if previous.handle.platform_task_id is not None:
                    try:
                        async for event in self._recover_bound(
                            previous, request.deadline_unix_seconds
                        ):
                            yield event
                    except ValueError as exc:
                        if exc.args != ("A2A_REMOTE_BINDING_UNKNOWN",):
                            raise
                        self._snapshot(
                            previous,
                            "unknown",
                            A2AInvocationError(
                                code="A2A_SEND_OUTCOME_UNKNOWN",
                                message="Remote send outcome is unknown",
                            ),
                        )
                return
            if previous.snapshot and previous.snapshot.terminal:
                return
        descriptor = ExecutionScopeDescriptor(
            scope_id=request.scope_id,
            parent_scope_id=request.handle.parent_scope_id,
            trigger_ref=trigger,
            binding_id=request.spec.binding_id,
            agent=PublicAgentRef(
                id=request.spec.target_agent_id,
                name=request.spec.local_name,
                version_id=request.spec.expected_version_id,
            ),
            status="submitted",
            started_at=float(self._clock()),
            cancel=ScopeCancelState(capability="unknown"),
        )
        state = previous or _State(request=request, handle=request.handle, descriptor=descriptor)
        state.active = True
        self._states[request.scope_id] = state
        state.tokens[request.idempotency_token] = request.message_sha256
        try:
            await self._save_state(state)
            # Save the immutable original request and explicit not-sent state
            # before a local approval pause. Recovery can cancel it locally or
            # re-authorize this exact request without inventing a new attempt.
            await self._authorize(request)
            prepared = await self._timed(
                self._control_plane.prepare_call(
                    space_id=request.spec.space_id,
                    target_agent_id=request.spec.target_agent_id,
                    expected_version_id=request.spec.expected_version_id,
                    message_id=request.message.message_id,
                    message_sha256=request.message_sha256,
                    idempotency_token=request.idempotency_token,
                ),
                request.deadline_unix_seconds,
                "prepare",
            )
            self._validate_prepared(request, prepared)
            await self._authorize(request, prepared.route.kind)
            state.handle = state.handle.with_platform_task(prepared.platform_task_id)
            if prepared.remote_task is not None:
                state.handle = state.handle.bind_remote_task(
                    prepared.remote_task.remote_task_id, prepared.remote_task.remote_context_id
                )
            descriptor_id = stable_item_id("a2a", state.handle.scope_id, "execution.scope")
            facts = await self._event_store.list(
                state.handle.session_id, run_id=state.handle.run_id
            )
            if not any(getattr(event, "item_id", None) == descriptor_id for event in facts):
                submitted = await self._descriptor_event(state, "submitted", initial=True)
                if prepared.remote_task is None:
                    yield submitted
            if prepared.remote_task is not None:
                # Durable receipt already bound: reconcile rather than submit twice.
                async for event in self._recover_bound(state, request.deadline_unix_seconds):
                    yield event
            else:
                async for event in self._send(
                    state,
                    prepared,
                    request.message.to_wire(),
                    request.deadline_unix_seconds,
                ):
                    yield event
        except Exception:
            if state.send_entered and state.snapshot is None:
                self._snapshot(
                    state,
                    "unknown",
                    A2AInvocationError(
                        code="A2A_SEND_OUTCOME_UNKNOWN",
                        message="Remote send outcome is unknown",
                        retryable=False,
                    ),
                )
            if state.snapshot is not None and state.snapshot.status == "unknown":
                # The local call failed; the remote outcome is still unknown.
                # Publish safe facts without converting that uncertainty into
                # a remote cancellation or permitting another send.
                item = stable_item_id("a2a", state.handle.scope_id, "outcome-error")
                common = dict(
                    schema_version=2,
                    seq=state.cursor + 1,
                    timestamp=float(self._clock()),
                    run_id=state.handle.run_id,
                    scope_id=state.handle.scope_id,
                    parent_scope_id=state.handle.parent_scope_id,
                    source=SourceRef(framework="a2a"),
                    item_id=item,
                    item_kind="status",
                )
                yield await self._publish(
                    state,
                    ItemStarted(
                        **common,
                        event_id=stable_event_id(
                            "a2a",
                            state.handle.scope_id,
                            item,
                            "item.started",
                            "error",
                            "outcome-error",
                            0,
                        ),
                        initial=ContentSnapshot(parts=()),
                    ),
                )
                common.update(seq=state.cursor + 1, timestamp=float(self._clock()))
                yield await self._publish(
                    state,
                    ItemFailed(
                        **common,
                        event_id=stable_event_id(
                            "a2a",
                            state.handle.scope_id,
                            item,
                            "item.failed",
                            "error",
                            "outcome-error",
                            0,
                        ),
                        error=ErrorInfo(
                            code="A2A_SEND_OUTCOME_UNKNOWN",
                            message="Remote result is unknown; do not resend the call.",
                            source="a2a",
                            scope_id=state.handle.scope_id,
                            item_id=item,
                        ),
                    ),
                )
                yield await self._descriptor_event(state, "failed")
            raise
        finally:
            state.active = False

    async def _send(self, state, prepared, message, deadline):
        lease = self._checkpoint_leases.get(state.handle.scope_id)
        if lease is not None and await lease.cancel_requested():
            if not state.send_entered:
                yield await self._descriptor_event(
                    state,
                    "cancelled",
                    cancel=ScopeCancelState(capability="unknown", request_state="local_confirmed"),
                )
                self._snapshot(state, "cancelled")
                await lease.clear_cancel_request()
            else:
                delivery = await self._cancel_delivery(state, deadline)
                for event in delivery.events:
                    yield event
            return
        async with AsyncExitStack() as stack:
            started_at = time.perf_counter()
            native_wait_seconds = 0.0
            reconcile_seconds = 0.0
            native_events = 0
            projected_events = 0
            # Entering the wire port is conservatively treated as possible send.
            state.send_entered = True
            await self._save_state(state)
            if state.descriptor.status != "working":
                yield await self._descriptor_event(state, "working")
            stream = await self._timed(
                stack.enter_async_context(
                    self._space_client.open_prepared_message_stream(prepared, message)
                ),
                deadline,
                "connect",
            )
            iterator = aiter(stream)
            stack.push_async_callback(self._stop_pending_read, state)
            first, terminal, direct = True, False, False
            while True:
                wait_started = time.perf_counter()
                try:
                    native = await self._timed(
                        self._next_native(state, iterator, deadline),
                        deadline,
                        "first_event" if first else "idle",
                    )
                except StopAsyncIteration:
                    native_wait_seconds += time.perf_counter() - wait_started
                    break
                native_wait_seconds += time.perf_counter() - wait_started
                if isinstance(native, _CancelDelivery):
                    for event in native.events:
                        yield event
                    if native.terminal:
                        return
                    continue
                native_events += 1
                first = False
                payload = (
                    getattr(native, native.WhichOneof("payload"))
                    if isinstance(native, StreamResponse) and native.WhichOneof("payload")
                    else native
                )
                await self._bind(state, payload, deadline)
                if isinstance(payload, Message) and not payload.context_id:
                    normalized = Message()
                    normalized.CopyFrom(payload)
                    normalized.context_id = state.context.context_id
                    payload = normalized
                events = state.adapter.map_event(
                    payload, state.context, timestamp=float(self._clock())
                )
                async for event in self._mapped(state, events):
                    projected_events += 1
                    yield event
                status = (
                    payload.status.state
                    if isinstance(payload, (Task, TaskStatusUpdateEvent))
                    else None
                )
                if status in {
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    TaskState.TASK_STATE_AUTH_REQUIRED,
                }:
                    state.source = "stream"
                    yield await self._descriptor_event(state, "input_required")
                    self._snapshot(
                        state,
                        "auth_required"
                        if status == TaskState.TASK_STATE_AUTH_REQUIRED
                        else "input_required",
                    )
                terminal |= status in {
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_CANCELED,
                    TaskState.TASK_STATE_REJECTED,
                }
                direct |= isinstance(payload, Message) and not state.handle.remote_task_id
            if state.handle.remote_task_id and (
                terminal or state.descriptor.status != "input_required"
            ):
                reconcile_started = time.perf_counter()
                async for event in self._observe_bound(state, deadline):
                    projected_events += 1
                    yield event
                reconcile_seconds = time.perf_counter() - reconcile_started
            elif direct:
                yield await self._descriptor_event(state, "completed")
                self._snapshot(state)
            elif state.descriptor.status != "input_required":
                self._snapshot(
                    state,
                    "unknown",
                    A2AInvocationError(
                        code="A2A_REMOTE_BINDING_UNKNOWN",
                        message="Remote task binding is not yet confirmed",
                        retryable=True,
                    ),
                )
            elapsed = time.perf_counter() - started_at
            logger.info(
                "A2A stream timing task=%s native_events=%d projected_events=%d "
                "elapsed_ms=%.1f native_wait_ms=%.1f consume_ms=%.1f reconcile_ms=%.1f",
                state.handle.platform_task_id,
                native_events,
                projected_events,
                elapsed * 1000,
                native_wait_seconds * 1000,
                max(0.0, elapsed - native_wait_seconds - reconcile_seconds) * 1000,
                reconcile_seconds * 1000,
            )

    async def _cancel_delivery(self, state, deadline):
        before = state.cursor
        result = await self._cancel(state.handle, deadline_unix_seconds=deadline)
        facts = await self._event_store.list(state.handle.session_id, run_id=state.handle.run_id)
        return _CancelDelivery(
            [
                event
                for event in facts
                if event.scope_id == state.handle.scope_id and event.seq > before
            ],
            bool(result.snapshot and result.snapshot.terminal),
        )

    async def _next_native(self, state, iterator, deadline):
        lease = self._checkpoint_leases.get(state.handle.scope_id)
        if lease is None:
            return await anext(iterator)
        pending = state.pending_native
        if pending is None:
            pending = state.pending_native = asyncio.create_task(anext(iterator))
        try:
            while True:
                key = (state.handle.platform_task_id, state.handle.remote_task_id)
                if state.cancel_checked_for != key and await lease.cancel_requested():
                    state.cancel_checked_for = key
                    delivery = await self._cancel_delivery(state, deadline)
                    if delivery.terminal:
                        await self._stop_pending_read(state)
                    return delivery
                done, _ = await asyncio.wait({pending}, timeout=0.2)
                if done:
                    state.pending_native = None
                    return pending.result()
        except BaseException:
            await self._stop_pending_read(state)
            raise

    @staticmethod
    async def _stop_pending_read(state):
        pending, state.pending_native = state.pending_native, None
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def _prepare_bound(self, state, operation, deadline, **kwargs):
        prepared = await self._timed(
            self._control_plane.prepare_task_operation(
                platform_task_id=state.handle.platform_task_id, operation=operation, **kwargs
            ),
            deadline,
            "prepare",
        )
        self._validate_prepared(state.request, prepared)
        if prepared.remote_task is None:
            raise ValueError("A2A_REMOTE_BINDING_UNKNOWN")
        state.handle = state.handle.bind_remote_task(
            prepared.remote_task.remote_task_id, prepared.remote_task.remote_context_id
        )
        # A bound get/cancel keeps the originally approved invocation identity.
        # Its separate I/O deadline must not invalidate that durable approval.
        await self._authorize(state.request, prepared.route.kind, deadline=deadline)
        return prepared

    async def _observe_bound(self, state, deadline):
        """Observe an existing task to the caller's original stopping boundary."""
        while True:
            lease = self._checkpoint_leases.get(state.handle.scope_id)
            if lease is not None and await lease.cancel_requested():
                delivery = await self._cancel_delivery(state, deadline)
                for event in delivery.events:
                    yield event
                if delivery.terminal:
                    return
            async for event in self._get(state, deadline):
                yield event
            if state.snapshot is not None and (
                state.snapshot.terminal
                or state.snapshot.status in {"input_required", "auth_required"}
            ):
                return
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("A2A_OVERALL_TIMEOUT")
            await asyncio.sleep(min(0.1, remaining))

    async def _recover_bound(self, state, deadline):
        # Adapter dedupe protects persistence, but a new consumer still needs
        # outputs committed before its stream began. Deliver existing facts in
        # canonical order before observing new facts, without appending again.
        await self._authorize(state.request, "hosted_gateway", deadline=deadline)
        delivered = set()
        for event in await self.replay(state.handle):
            delivered.add(event.event_id)
            yield event
        async for event in self._observe_bound(state, deadline):
            if event.event_id not in delivered:
                delivered.add(event.event_id)
                yield event
        if state.snapshot is not None and state.snapshot.terminal:
            await self._terminal_replay(state)

    async def _get(self, state, deadline):
        prepared = await self._prepare_bound(state, "get_task", deadline)
        task = await self._timed(
            self._space_client.get_prepared_task(prepared), deadline, "connect"
        )
        await self._bind(state, task, deadline)

        class Fetched:
            async def get_task(self, request):
                return task

        result = await state.adapter.reconcile(
            Fetched(), state.context, reason="terminal", timestamp=float(self._clock())
        )
        if not result.consistent:
            raise ValueError(result.error or "A2A_RECONCILIATION_FAILED")
        async for event in self._mapped(state, result.events):
            yield event
        state.source = "get_task"
        statuses = {
            TaskState.TASK_STATE_COMPLETED: "completed",
            TaskState.TASK_STATE_FAILED: "failed",
            TaskState.TASK_STATE_REJECTED: "failed",
            TaskState.TASK_STATE_CANCELED: "cancelled",
            TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
            TaskState.TASK_STATE_AUTH_REQUIRED: "input_required",
        }
        status = statuses.get(task.status.state, "working")
        if state.descriptor.status in {"completed", "failed", "cancelled"}:
            if state.snapshot is not None and not state.snapshot.terminal:
                # Reconcile the private remote result without reopening the
                # already terminal public local call.
                self._snapshot(state, status)
            return
        cancel = (
            ScopeCancelState(capability="supported", request_state="confirmed")
            if status == "cancelled"
            else None
        )
        if status != state.descriptor.status or cancel is not None:
            yield await self._descriptor_event(state, status, cancel=cancel)
        error = (
            A2AInvocationError(code="A2A_REMOTE_FAILED", message="Remote task failed")
            if status == "failed"
            else None
        )
        self._snapshot(state, status, error)

    async def get(self, handle, *, deadline_unix_seconds=None):
        async with self._operation(handle):
            return await self._get_snapshot(handle, deadline_unix_seconds=deadline_unix_seconds)

    async def _get_snapshot(self, handle, *, deadline_unix_seconds=None):
        state = self._state(handle)
        if state.snapshot and state.snapshot.terminal and not state.send_entered:
            return state.snapshot
        lease = self._checkpoint_leases.get(handle.scope_id)
        if (
            lease is not None
            and await lease.cancel_requested()
            and not (state.snapshot and state.snapshot.terminal)
        ):
            await self._cancel(state.handle, deadline_unix_seconds=deadline_unix_seconds)
            if state.snapshot and state.snapshot.terminal:
                return state.snapshot
        async for _ in self._get(state, deadline_unix_seconds or float(self._clock()) + 60):
            pass
        return state.snapshot

    def _state(self, handle):
        state = self._states.get(handle.scope_id)
        if state is None:
            raise KeyError("A2A_INVOCATION_NOT_FOUND")
        # Immutable identity must match; an older pre-bind handle is accepted.
        if state.handle.model_dump(
            exclude={"platform_task_id", "remote_task_id", "remote_context_id"}
        ) != handle.model_dump(exclude={"platform_task_id", "remote_task_id", "remote_context_id"}):
            raise ValueError("A2A_HANDLE_CONFLICT")
        # A pre-bind handle may omit remote locators. Once a caller supplies
        # one, it must agree with the authoritative receipt; never silently
        # retarget a stale or cross-task handle to this scope's cached task.
        for locator_field in ("platform_task_id", "remote_task_id", "remote_context_id"):
            supplied = getattr(handle, locator_field)
            if supplied is not None and supplied != getattr(state.handle, locator_field):
                raise ValueError("A2A_HANDLE_CONFLICT")
        return state

    async def continue_task(self, request: A2AContinuationRequest):
        async with self._operation(request.handle):
            async for event in self._continue_task(request):
                yield event

    async def _continue_task(self, request: A2AContinuationRequest):
        state = self._state(request.handle)
        deadline = min(request.deadline_unix_seconds, state.request.deadline_unix_seconds)
        if state.active:
            raise ValueError("A2A_INVOCATION_BUSY")
        if request.spec != state.request.spec:
            raise ValueError("A2A_TARGET_MISMATCH")
        message = request.to_wire_message()
        digest = canonical_message_sha256(message)
        if request.idempotency_token in state.tokens:
            if state.tokens[request.idempotency_token] != digest:
                raise ValueError("A2A_IDEMPOTENCY_CONFLICT")
            await self._authorize(request, deadline=deadline)
            if state.snapshot is not None and state.snapshot.terminal:
                for event in await self._terminal_replay(state):
                    yield event
            else:
                async for event in self._recover_bound(
                    state, deadline
                ):
                    yield event
            state.pending_continuation = None
            return
        if state.descriptor.status != "input_required":
            raise ValueError("A2A_CONTINUATION_NOT_ALLOWED")
        if state.pending_continuation is not None and state.pending_continuation != request:
            raise ValueError("A2A_PENDING_CONTINUATION_CONFLICT")
        state.pending_continuation = request
        await self._save_state(state)
        await self._authorize(request, deadline=deadline)
        state.active = True
        try:
            binding = await self._prepare_bound(state, "get_task", deadline)
            if (
                request.handle.remote_task_id != state.handle.remote_task_id
                or request.handle.remote_context_id != state.handle.remote_context_id
            ):
                raise ValueError("A2A_REMOTE_BINDING_CONFLICT")
            prepared = await self._prepare_bound(
                state,
                "send_message",
                deadline,
                message_id=message.message_id,
                message_sha256=digest,
                idempotency_token=request.idempotency_token,
            )
            if prepared.remote_task != binding.remote_task:
                raise ValueError("A2A_REMOTE_BINDING_CONFLICT")
            state.tokens[request.idempotency_token] = digest
            async for event in self._send(state, prepared, message, deadline):
                yield event
            state.pending_continuation = None
        finally:
            state.active = False

    async def cancel(self, handle, *, deadline_unix_seconds=None):
        from ksadk.a2a.invocation_checkpoint import InvocationCheckpointBusy

        try:
            async with self._operation(handle):
                return await self._cancel(handle, deadline_unix_seconds=deadline_unix_seconds)
        except InvocationCheckpointBusy:
            requested = float(self._clock())
            saved = await self._checkpoint_store.request_cancel(handle)
            state = self._restore_state(saved)
            return A2ACancelResult(
                handle=state.handle,
                outcome="already_terminal"
                if state.snapshot and state.snapshot.terminal
                else "cancel_requested_but_remote_unknown",
                requested_at_unix_seconds=requested,
                resolved_at_unix_seconds=float(self._clock()),
                snapshot=state.snapshot,
            )

    async def _cancel(self, handle, *, deadline_unix_seconds=None):
        state = self._state(handle)
        requested = float(self._clock())
        deadline = deadline_unix_seconds or requested + 60
        if state.snapshot and state.snapshot.terminal:
            outcome = "already_terminal"
        elif not state.send_entered and state.handle.remote_task_id is None and not state.active:
            if state.cursor == 0:
                await self._descriptor_event(state, "submitted", initial=True)
            await self._descriptor_event(
                state,
                "cancelled",
                cancel=ScopeCancelState(capability="unknown", request_state="local_confirmed"),
            )
            self._snapshot(state, "cancelled")
            outcome = "local_cancelled_before_send"
        else:
            outcome = "cancel_requested_but_remote_unknown"
            try:
                prepared = await self._prepare_bound(
                    state,
                    "cancel_task",
                    deadline,
                    idempotency_token=stable_item_id("a2a", state.handle.scope_id, "cancel"),
                )
                task = await self._timed(
                    self._space_client.cancel_prepared_task(prepared), deadline, "connect"
                )
                if task.status.state == TaskState.TASK_STATE_CANCELED:
                    async for _ in self._get(state, deadline):
                        pass
                    if state.snapshot and state.snapshot.status == "cancelled":
                        outcome = "remote_cancelled"
                elif task.status.state in {
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_REJECTED,
                }:
                    async for _ in self._get(state, deadline):
                        pass
                    outcome = "already_terminal"
            except Exception as exc:
                if type(exc).__name__ in {"TaskNotCancelableError", "UnsupportedOperationError"}:
                    outcome = "cancel_requested_but_remote_unsupported"
            if outcome.startswith("cancel_requested"):
                unsupported = outcome.endswith("unsupported")
                await self._descriptor_event(
                    state,
                    state.descriptor.status,
                    cancel=ScopeCancelState(
                        capability="unsupported" if unsupported else "unknown",
                        request_state="unsupported" if unsupported else "unknown",
                    ),
                )
        lease = self._checkpoint_leases.get(state.handle.scope_id)
        if lease is not None and outcome in {
            "local_cancelled_before_send",
            "remote_cancelled",
            "already_terminal",
            "cancel_requested_but_remote_unsupported",
        }:
            await lease.clear_cancel_request()
        return A2ACancelResult(
            handle=state.handle,
            outcome=outcome,
            requested_at_unix_seconds=requested,
            resolved_at_unix_seconds=float(self._clock()),
            snapshot=state.snapshot,
        )
