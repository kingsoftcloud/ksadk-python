"""Fixed Codex experts using native tool identities and the shared A2A service."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from contextlib import aclosing
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from ksadk.a2a.timeouts import invocation_seconds
from ksadk.a2a.exposure import compile_exposure_plan
from ksadk.a2a.invocation_types import (
    A2AInvocationContext, A2AInvocationHandle, A2AInvocationMessage,
    A2AInvocationRequest, RemoteAgentSpec,
)
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.a2a.result_text import select_terminal_reply
from ksadk.a2a.runtime_input import invoke_with_input
from ksadk.events.content import TextContent, ToolCallContent
from ksadk.events.identity import stable_item_id


class CodexToolCall(BaseModel):
    """The app-server item/tool/call request, never model-selected identity."""
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    thread_id: str = Field(alias="threadId", min_length=1)
    turn_id: str = Field(alias="turnId", min_length=1)
    call_id: str = Field(alias="callId", min_length=1)
    tool: str = Field(min_length=1)
    namespace: None = None
    arguments: dict


@dataclass(frozen=True)
class CodexExpertInvocation:
    request: A2AInvocationRequest
    trigger: ScopeTriggerRef
    service: object


class CodexInvocationContextProvider:
    def __init__(self, *, authority, service_for_space, caller_agent_id, clock=time.time, require_durable=False):
        self.authority, self.services = authority, service_for_space
        self.caller, self.clock = caller_agent_id, clock
        self.require_durable = require_durable

    async def resolve(self, spec, call):
        authority = self.authority
        authority.require_session(authority.session_id)
        triggers = set()
        for event in await authority.list(authority.session_id):
            source = event.source
            if (source.framework != "codex" or source.native_run_id != call.turn_id
                    or source.native_item_id != call.call_id
                    or source.metadata.get("thread_id") != call.thread_id
                    or source.metadata.get("turn_id") != call.turn_id):
                continue
            snapshot = getattr(event, "initial", None) or getattr(event, "snapshot", None)
            if snapshot is None or getattr(event, "item_kind", None) != "tool_call":
                continue
            for part in snapshot.parts:
                if (isinstance(part, ToolCallContent) and part.call_id == call.call_id
                        and part.name == spec.local_name
                        and part.arguments == {"arguments": call.arguments, "namespace": None}):
                    triggers.add((event.scope_id, event.item_id, part.call_id))
        if len(triggers) != 1:
            raise ValueError("A2A_CODEX_NATIVE_TRIGGER_NOT_PERSISTED")
        scope, item, native_call = next(iter(triggers))
        trigger = ScopeTriggerRef(scope_id=scope, item_id=item, call_id=native_call)
        handle = A2AInvocationHandle(
            run_id=authority.run_id, session_id=authority.session_id,
            parent_scope_id=scope, call_id=native_call, binding_id=spec.binding_id,
            target_agent_id=spec.target_agent_id,
            attempt_id=stable_item_id("a2a", scope, item, "attempt-1"),
        )
        service = self.services(spec.space_id)
        if inspect.isawaitable(service):
            service = await service
        if self.require_durable and not service.durable_recovery_available:
            raise ValueError("A2A_CODEX_DURABLE_SERVICE_REQUIRED")
        original = await service.original_request(handle)
        message = call.arguments["message"]
        if original is not None:
            if (original.spec != spec or original.context.caller_agent_id != self.caller
                    or original.message.text_parts != (message,)):
                raise ValueError("A2A_RECOVERED_BINDING_CONFLICT")
            return CodexExpertInvocation(original, trigger, service)
        request = A2AInvocationRequest(
            spec=spec, handle=handle,
            message=A2AInvocationMessage(message_id=stable_item_id("a2a", handle.scope_id, "initial-message"), text_parts=(message,)),
            context=A2AInvocationContext(caller_agent_id=self.caller, audit_id=stable_item_id("a2a", handle.scope_id, "audit")),
            deadline_unix_seconds=float(self.clock()) + invocation_seconds(spec.timeout_seconds),
            idempotency_token=stable_item_id("a2a", handle.scope_id, "initial-send"),
        )
        return CodexExpertInvocation(request, trigger, service)


@dataclass(frozen=True)
class CodexExpert:
    spec: RemoteAgentSpec

    def __post_init__(self):
        spec = RemoteAgentSpec.model_validate(self.spec.model_dump())
        compile_exposure_plan([spec], framework="codex", consumer_id=spec.local_name)
        object.__setattr__(self, "spec", spec)

    def tool_spec(self):
        """Pinned app-server experimental dynamicTools function declaration."""
        return {"type": "function", "name": self.spec.local_name,
                "description": self.spec.description or "Ask this fixed remote expert and receive its result.",
                "inputSchema": {"type": "object", "properties": {"message": {"type": "string", "minLength": 1}},
                                "required": ["message"], "additionalProperties": False}}

    async def invoke(self, call, *, provider: CodexInvocationContextProvider, emit,
                     before_invoke=None):
        call = CodexToolCall.model_validate(call.model_dump() if isinstance(call, CodexToolCall) else call)
        if call.tool != self.spec.local_name or set(call.arguments) != {"message"}:
            raise ValueError("A2A_FIXED_TARGET_ARGUMENT_INVALID")
        if not isinstance(call.arguments["message"], str) or not call.arguments["message"].strip():
            raise ValueError("A2A_MESSAGE_REQUIRED")
        invocation = await provider.resolve(self.spec, call)
        if before_invoke is not None:
            await before_invoke(call)
        service, request, trigger = invocation.service, invocation.request, invocation.trigger
        texts, kinds = {}, {}
        try:
            async with aclosing(invoke_with_input(service, request, trigger_ref=trigger)) as events:
                async for event in events:
                    published = emit(event)
                    if inspect.isawaitable(published):
                        await published
                    if event.event_type == "item.completed" and event.item_kind in {"message", "artifact"}:
                        key = (event.scope_id, event.item_id)
                        kinds[key] = event.item_kind
                        texts[key] = {part.part_id: part.text for part in event.snapshot.parts if isinstance(part, TextContent)}
        except (asyncio.CancelledError, TimeoutError):
            try:
                handle = service.handle(request.scope_id)
            except KeyError:
                pass
            else:
                await asyncio.shield(service.cancel(handle))
            raise
        snapshot = service.snapshot(service.handle(request.scope_id))
        if snapshot is None:
            raise RuntimeError("A2A_INVOCATION_NOT_COMPLETED")
        result = {"ok": snapshot.status == "completed", "state": snapshot.status,
                  "reply": select_terminal_reply(snapshot.output_refs, texts, kinds)}
        return {"success": result["ok"], "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}


def codex_expert(spec: RemoteAgentSpec) -> CodexExpert:
    return CodexExpert(spec)


def compile_codex_experts(specs):
    plan = compile_exposure_plan(specs, framework="codex", consumer_id="codex")
    return tuple(codex_expert(entry.spec) for entry in plan.entries)
