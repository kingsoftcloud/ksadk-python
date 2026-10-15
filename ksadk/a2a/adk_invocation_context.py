"""Native ADK trigger identities used by the managed invocation provider."""

import inspect
import time
from dataclasses import dataclass

from ksadk.a2a.invocation_types import (
    A2AInvocationContext,
    A2AInvocationHandle,
    A2AInvocationMessage,
    A2AInvocationRequest,
)
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.events.identity import stable_item_id, stable_scope_id


class ADKWorkflowTrigger:
    """Persist a trigger for a static native child slot (sequential/parallel).

    Loops require explicit occurrence injection and are outside this selector.
    Native handoffs use their real function-call event instead.
    """

    def __init__(self, authority, *, clock=time.time):
        self._authority, self._clock = authority, clock

    async def __call__(self, ctx, spec):
        from google.adk.agents import LoopAgent

        from ksadk.events.canonical import ItemCompleted, ItemStarted, SourceRef
        from ksadk.events.content import ContentSnapshot, ToolCallContent
        from ksadk.events.identity import stable_event_id, stable_part_id

        authority = self._authority
        authority.require_session(authority.session_id)
        node = ctx.agent
        if getattr(node, "remote_spec", None) != spec or not ctx.invocation_id:
            raise ValueError("A2A_NATIVE_EXECUTION_REQUIRED")
        path, seen = [], set()
        while node is not None:
            if id(node) in seen or len(seen) >= 64:
                raise ValueError("A2A_NATIVE_EXECUTION_PATH_INVALID")
            seen.add(id(node))
            if isinstance(node, LoopAgent):
                raise ValueError("A2A_WORKFLOW_OCCURRENCE_REQUIRED")
            parent = node.parent_agent
            slot = (
                0
                if parent is None
                else next(
                    (index for index, child in enumerate(parent.sub_agents) if child is node), -1
                )
            )
            if slot < 0:
                raise ValueError("A2A_NATIVE_EXECUTION_PATH_INVALID")
            path.append(f"{slot}:{node.name}")
            node = parent
        native_slot = "/".join(reversed(path))
        scope = stable_scope_id("adk", authority.run_id)
        call = stable_item_id(
            "adk", ctx.invocation_id, ctx.branch or "$root", native_slot, "native-child-call"
        )
        item = stable_item_id("adk", scope, call, "tool_call")
        trigger = ScopeTriggerRef(scope_id=scope, item_id=item, call_id=call)
        part = stable_part_id("adk", item, "call")
        snapshot = ContentSnapshot(
            parts=(ToolCallContent(part_id=part, call_id=call, name=spec.local_name, arguments={}),)
        )
        stored = {event.event_id: event for event in await authority.list(authority.session_id)}
        prefix = []
        for event_type in ("item.started", "item.completed"):
            event_id = stable_event_id("adk", scope, item, event_type, part, call, 0)
            existing = stored.get(event_id)
            common = dict(
                schema_version=2,
                seq=0,
                timestamp=(existing.timestamp if existing else self._clock()),
                event_id=event_id,
                run_id=authority.run_id,
                scope_id=scope,
                item_id=item,
                item_kind="tool_call",
                source=SourceRef(
                    framework="adk",
                    native_run_id=ctx.invocation_id,
                    metadata={"execution_kind": "remote_agent"},
                ),
            )
            event = (
                ItemStarted(**common, initial=snapshot, phase="commentary")
                if event_type == "item.started"
                else ItemCompleted(**common, snapshot=snapshot)
            )
            persisted, _ = await authority.persist_one(authority.session_id, event)
            prefix.append(persisted)
        return trigger, tuple(prefix)


@dataclass(frozen=True)
class ManagedADKInvocationContext:
    request: A2AInvocationRequest
    trigger: ScopeTriggerRef
    service: object
    prefix_events: tuple = ()


class ADKInvocationContextProvider:
    """Run-local composition of identity, message selection, and durable request.

    Selectors are runtime-owned functions, not fields read from model input.
    A workflow selector returns an explicit trigger and persisted prefix facts.
    """

    def __init__(
        self,
        *,
        authority,
        service_for_space,
        caller_agent_id,
        message_selector,
        workflow_trigger=None,
        clock=time.time,
    ):
        if not caller_agent_id:
            raise ValueError("A2A_CALLER_REQUIRED")
        self._authority, self._services = authority, service_for_space
        self._caller, self._message = caller_agent_id, message_selector
        self._workflow = workflow_trigger or ADKWorkflowTrigger(authority, clock=clock)
        self._clock = clock

    async def __call__(self, ctx, spec):
        authority = self._authority
        authority.require_session(authority.session_id)
        trigger = native_handoff_trigger(ctx, spec)
        prefix = ()
        if trigger is None:
            if self._workflow is None:
                raise ValueError("A2A_NATIVE_TRIGGER_REQUIRED")
            result = self._workflow(ctx, spec)
            if inspect.isawaitable(result):
                result = await result
            trigger, prefix = result
        if not isinstance(trigger, ScopeTriggerRef):
            raise TypeError("A2A_TRIGGER_REF_REQUIRED")
        handle = A2AInvocationHandle(
            run_id=authority.run_id,
            session_id=authority.session_id,
            parent_scope_id=trigger.scope_id,
            call_id=trigger.call_id,
            binding_id=spec.binding_id,
            target_agent_id=spec.target_agent_id,
            attempt_id=stable_item_id("a2a", trigger.scope_id, trigger.item_id, "attempt-1"),
        )
        service = self._services(spec.space_id)
        if inspect.isawaitable(service):
            service = await service
        original = await service.original_request(handle)
        if original is not None:
            if original.spec != spec or original.context.caller_agent_id != self._caller:
                raise ValueError("A2A_RECOVERED_BINDING_CONFLICT")
            return ManagedADKInvocationContext(original, trigger, service, tuple(prefix))
        parts = self._message(ctx, spec)
        if inspect.isawaitable(parts):
            parts = await parts
        request = A2AInvocationRequest(
            spec=spec,
            handle=handle,
            message=A2AInvocationMessage(
                message_id=stable_item_id("a2a", handle.scope_id, "initial-message"),
                text_parts=parts,
            ),
            context=A2AInvocationContext(
                caller_agent_id=self._caller,
                audit_id=stable_item_id("a2a", handle.scope_id, "audit"),
            ),
            deadline_unix_seconds=float(self._clock()) + (spec.timeout_seconds or 300.0),
            idempotency_token=stable_item_id("a2a", handle.scope_id, "initial-send"),
        )
        return ManagedADKInvocationContext(request, trigger, service, tuple(prefix))


def _path(event):
    return (
        getattr(getattr(event, "node_info", None), "path", None)
        or getattr(event, "branch", None)
        or "$root"
    )


def native_handoff_trigger(ctx, spec):
    """Resolve a real transfer response back to its exact canonical call item.

    No text/argument hash or target-name-derived call ID is used. Non-handoff
    execution returns None so its workflow trigger can be allocated explicitly.
    """
    events = list(ctx.session.events)
    for index in range(len(events) - 1, -1, -1):
        response = events[index]
        if response.invocation_id != ctx.invocation_id or getattr(
            response, "branch", None
        ) != getattr(ctx, "branch", None):
            continue
        if getattr(response.actions, "transfer_to_agent", None) != spec.local_name:
            return None
        calls = [
            part.function_response
            for part in (getattr(response.content, "parts", None) or ())
            if part.function_response is not None
            and part.function_response.name == "transfer_to_agent"
        ]
        if len(calls) != 1 or not calls[0].id:
            raise ValueError("A2A_NATIVE_TRIGGER_AMBIGUOUS")
        call_id = calls[0].id
        for event in reversed(events[:index]):
            if (
                event.invocation_id != ctx.invocation_id
                or _path(event) != _path(response)
                or event.author != response.author
            ):
                continue
            matches = [
                part.function_call
                for part in (getattr(event.content, "parts", None) or ())
                if part.function_call is not None
                and part.function_call.id == call_id
                and part.function_call.name == "transfer_to_agent"
            ]
            if len(matches) > 1:
                raise ValueError("A2A_NATIVE_TRIGGER_AMBIGUOUS")
            if matches:
                if not event.id:
                    raise ValueError("A2A_NATIVE_TRIGGER_ID_REQUIRED")
                path = _path(event)
                return ScopeTriggerRef(
                    scope_id=stable_scope_id("adk", event.invocation_id, path),
                    item_id=stable_item_id(
                        "adk", event.invocation_id, path, event.id, call_id, "tool_call"
                    ),
                    call_id=call_id,
                )
        raise ValueError("A2A_NATIVE_TRIGGER_CALL_MISSING")
    return None
