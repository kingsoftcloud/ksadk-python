"""Fixed LangGraph call surfaces backed only by the managed invocation service.

The runtime binds the provider and canonical sink for each execution. Graph
state and RunnableConfig are inputs, never an authorization or route source.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool

from ksadk.a2a.exposure import compile_exposure_plan
from ksadk.a2a.invocation_types import A2AInvocationRequest, RemoteAgentSpec
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.a2a.result_text import select_terminal_reply
from ksadk.a2a.runtime_input import invoke_with_input
from ksadk.events.content import TextContent


@dataclass(frozen=True)
class _Binding:
    service: Any
    provider: Callable
    emit: Callable
    runtime: Any = None


_binding: ContextVar[_Binding | None] = ContextVar("managed_langgraph_a2a", default=None)


@contextmanager
def bind_managed_langgraph(service, provider, emit, *, runtime=None):
    """Bind trusted execution dependencies; child async tasks inherit them."""
    token = _binding.set(_Binding(service, provider, emit, runtime))
    try:
        yield
    finally:
        _binding.reset(token)


def _fixed_spec(spec: RemoteAgentSpec) -> RemoteAgentSpec:
    spec = RemoteAgentSpec.model_validate(spec.model_dump())
    compile_exposure_plan([spec], framework="langgraph", consumer_id=spec.local_name)
    return spec


async def _invoke(spec, payload, config, *, surface, result_envelope=False):
    binding = _binding.get()
    if binding is None:
        raise RuntimeError("A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED")
    from ksadk.a2a.tool_outcomes import assert_target_outcome_known, unknown_service_outcome

    if binding.runtime is not None:
        await assert_target_outcome_known(binding.runtime.authority, spec.target_agent_id)
    resolved = binding.provider(spec, payload, config, surface)
    if inspect.isawaitable(resolved):
        resolved = await resolved
    from ksadk.a2a.langgraph_invocation_context import ManagedLangGraphInvocation

    prefix = ()
    service = binding.service
    if isinstance(resolved, ManagedLangGraphInvocation):
        request, trigger, service = resolved.request, resolved.trigger, resolved.service
        prefix = resolved.prefix_events
    else:
        request, trigger = resolved
    if not isinstance(request, A2AInvocationRequest) or not isinstance(trigger, ScopeTriggerRef):
        raise TypeError("A2A_MANAGED_INVOCATION_CONTEXT_INVALID")
    if request.spec != spec:
        raise ValueError("A2A_BINDING_MISMATCH")
    if service is None:
        raise RuntimeError("A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED")
    for event in prefix:
        if event.run_id != request.handle.run_id or event.scope_id != trigger.scope_id:
            raise ValueError("A2A_TRIGGER_REF_CONFLICT")
        emitted = binding.emit(event)
        if inspect.isawaitable(emitted):
            await emitted
    snapshots, item_kinds = {}, {}
    try:
        async with aclosing(
            invoke_with_input(service, request, trigger_ref=trigger)
        ) as events:
            async for event in events:
                # Forward the original fact, including private live waiters.
                # Serializing/recreating it here would break approval/input.
                emitted = binding.emit(event)
                if inspect.isawaitable(emitted):
                    await emitted
                snapshot = getattr(event, "snapshot", None) or getattr(event, "initial", None)
                if snapshot is not None and event.item_kind in {"message", "artifact"}:
                    key = (event.scope_id, event.item_id)
                    item_kinds[key] = event.item_kind
                    snapshots[key] = {
                        part.part_id: part.text
                        for part in snapshot.parts if isinstance(part, TextContent)
                    }
    except asyncio.CancelledError:
        try:
            await asyncio.shield(service.cancel(service.handle(request.scope_id)))
        finally:
            raise
    except Exception as exc:
        unknown = unknown_service_outcome(service, request.scope_id)
        if unknown is not None:
            raise unknown from exc
        if isinstance(exc, TimeoutError):
            try:
                await asyncio.shield(service.cancel(service.handle(request.scope_id)))
            finally:
                raise
        raise
    handle = service.handle(request.scope_id)
    latest = service.snapshot(handle)
    if unknown := unknown_service_outcome(service, request.scope_id):
        raise unknown
    if latest is None or latest.status != "completed":
        raise RuntimeError("A2A_INVOCATION_NOT_COMPLETED")
    reply = select_terminal_reply(latest.output_refs, snapshots, item_kinds)
    if result_envelope:
        return {"ok": True, "agent": spec.target_agent_id,
                "task_id": handle.platform_task_id, "state": latest.status, "reply": reply}
    return reply


def remote_node(spec: RemoteAgentSpec):
    """Return an async StateGraph node with one immutable remote target."""
    spec = _fixed_spec(spec)

    async def node(state: dict, config: RunnableConfig):
        text = await _invoke(spec, state, config, surface="node")
        return {"messages": [AIMessage(content=text, name=spec.local_name)]}

    node.__name__ = spec.local_name
    node.remote_spec = spec
    return node


def handoff_tool(spec: RemoteAgentSpec) -> StructuredTool:
    """Return a supervisor tool that delegates to a fixed remote and returns.

    This is not a jump into the remote graph or a native remote subgraph.
    """
    spec = _fixed_spec(spec)

    async def invoke(message: str, config: RunnableConfig) -> str:
        if not message.strip():
            raise ValueError("A2A_MESSAGE_REQUIRED")
        return await _invoke(spec, {"message": message}, config, surface="handoff")

    from ksadk.a2a.langgraph_tools import ManagedLangGraphTool

    return ManagedLangGraphTool.from_function(
        coroutine=invoke,
        name=spec.local_name,
        description=spec.description or "Delegate a task to this fixed remote expert.",
        metadata={"ksadk_a2a_remote_spec": spec.model_dump(mode="json")},
    )
