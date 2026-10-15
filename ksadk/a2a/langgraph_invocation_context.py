"""Runtime-owned LangGraph node identity and durable invocation construction."""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any

from ksadk.a2a.invocation_types import (
    A2AInvocationContext, A2AInvocationHandle, A2AInvocationMessage, A2AInvocationRequest,
)
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.events.adapters._langgraph_support import _scope_id
from ksadk.events.canonical import ItemCompleted, ItemStarted, SourceRef
from ksadk.events.content import ContentSnapshot, ToolCallContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_part_id


@dataclass(frozen=True)
class ManagedLangGraphInvocation:
    request: A2AInvocationRequest
    trigger: ScopeTriggerRef
    service: Any
    prefix_events: tuple = ()


class LangGraphInvocationContextProvider:
    def __init__(self, *, authority, service_for_space, caller_agent_id, clock=time.time, tool_trigger=None):
        if not caller_agent_id:
            raise ValueError("A2A_CALLER_REQUIRED")
        self.authority = authority
        self.services = service_for_space
        self.caller = caller_agent_id
        self.clock = clock
        self.tool_trigger = tool_trigger

    async def __call__(self, spec, payload, config, surface):
        authority = self.authority
        authority.require_session(authority.session_id)
        if surface != "node" and self.tool_trigger is None:
            # Tool handoffs must bind the actual native tool-call item, not
            # allocate a synthetic node call in place of a missing identity.
            raise ValueError("A2A_NATIVE_TOOL_TRIGGER_REQUIRED")
        configurable = config.get("configurable") or {}
        metadata = config.get("metadata") or {}
        task_id = str(configurable.get("__pregel_task_id") or "")
        node = str(metadata.get("langgraph_node") or "")
        namespace = metadata.get("langgraph_checkpoint_ns")
        if not task_id or not node or not isinstance(namespace, str):
            raise ValueError("A2A_NATIVE_EXECUTION_REQUIRED")
        components = tuple(namespace.split("|")) if namespace else ()
        if not components or components[-1] != f"{node}:{task_id}":
            raise ValueError("A2A_NATIVE_EXECUTION_PATH_INVALID")
        if surface != "node":
            trigger = await self.tool_trigger(spec, config)
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("A2A_MESSAGE_REQUIRED")
            return await self._request(spec, trigger, (), (message,))
        scope = _scope_id(authority.run_id, components)
        call = stable_item_id("langgraph", authority.run_id, namespace, task_id, "a2a-node")
        item = stable_item_id("langgraph", scope, call, "tool_call")
        trigger = ScopeTriggerRef(scope_id=scope, item_id=item, call_id=call)
        part = stable_part_id("langgraph", item, "call")
        snapshot = ContentSnapshot(parts=(ToolCallContent(
            part_id=part, call_id=call, name=spec.local_name, arguments={},
        ),))
        previous = {e.event_id: e for e in await authority.list(authority.session_id)}
        prefix = []
        for kind in ("item.started", "item.completed"):
            event_id = stable_event_id("langgraph", scope, item, kind, part, call, 0)
            existing = previous.get(event_id)
            common = dict(
                schema_version=2, seq=0,
                timestamp=existing.timestamp if existing else self.clock(),
                event_id=event_id, run_id=authority.run_id, scope_id=scope,
                item_id=item, item_kind="tool_call",
                source=SourceRef(
                    framework="langgraph", native_run_id=task_id,
                    native_item_id=node,
                    metadata={"execution_kind": "remote_node", "namespace": namespace,
                              "node_id": node, "binding_id": spec.binding_id},
                ),
            )
            event = (ItemStarted(**common, initial=snapshot, phase="commentary")
                     if kind == "item.started" else ItemCompleted(**common, snapshot=snapshot))
            saved, _ = await authority.persist_one(authority.session_id, event)
            prefix.append(saved)
        parts = []
        # Only visible user/model text becomes remote input. Tool payloads,
        # reasoning blocks and arbitrary state/config fields stay local.
        for message in payload.get("messages", ()):
            if getattr(message, "type", None) not in {"human", "ai"}:
                continue
            content = message.content
            if isinstance(content, str) and content.strip():
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(block["text"] for block in content
                             if isinstance(block, dict) and block.get("type") == "text"
                             and isinstance(block.get("text"), str) and block["text"].strip())
        return await self._request(spec, trigger, prefix, tuple(parts))

    async def _request(self, spec, trigger, prefix, parts):
        authority = self.authority
        handle = A2AInvocationHandle(
            run_id=authority.run_id, session_id=authority.session_id,
            parent_scope_id=trigger.scope_id, call_id=trigger.call_id, binding_id=spec.binding_id,
            target_agent_id=spec.target_agent_id,
            attempt_id=stable_item_id("a2a", trigger.scope_id, trigger.item_id, "attempt-1"),
        )
        service = self.services(spec.space_id)
        if inspect.isawaitable(service):
            service = await service
        original = await service.original_request(handle)
        if original is not None:
            if original.spec != spec or original.context.caller_agent_id != self.caller:
                raise ValueError("A2A_RECOVERED_BINDING_CONFLICT")
            return ManagedLangGraphInvocation(original, trigger, service, tuple(prefix))
        if not parts:
            raise ValueError("A2A_MESSAGE_REQUIRED")
        request = A2AInvocationRequest(
            spec=spec, handle=handle,
            message=A2AInvocationMessage(
                message_id=stable_item_id("a2a", handle.scope_id, "initial-message"),
                text_parts=tuple(parts),
            ),
            context=A2AInvocationContext(
                caller_agent_id=self.caller,
                audit_id=stable_item_id("a2a", handle.scope_id, "audit"),
            ),
            deadline_unix_seconds=float(self.clock()) + (spec.timeout_seconds or 300.0),
            idempotency_token=stable_item_id("a2a", handle.scope_id, "initial-send"),
        )
        return ManagedLangGraphInvocation(request, trigger, service, tuple(prefix))
