"""Code-first managed ADK child with explicit invocation identity injection."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.genai import types
from pydantic import PrivateAttr

from ksadk.a2a.exposure import compile_exposure_plan
from ksadk.a2a.invocation_types import A2AInvocationRequest, RemoteAgentSpec
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.events.canonical import InteractionRequested, ItemCompleted, ItemSnapshotReplaced, ItemUpdated
from ksadk.events.content import TextContent


@dataclass(frozen=True)
class ManagedA2ABinding:
    """Run-local dependencies; never place this object in session state.

    provider(ctx, spec) returns (A2AInvocationRequest, ScopeTriggerRef), optionally
    awaitable. It supplies the actual public root run/session ID, actual parent
    scope and triggering canonical item/call ID, binding/attempt IDs, caller and
    audit identities, stable wire message/idempotency IDs and absolute deadline.
    """

    invocation_service: Any
    invocation_context_provider: Callable


_binding: ContextVar[ManagedA2ABinding | None] = ContextVar("managed_a2a_binding", default=None)


@contextmanager
def bind_managed_a2a(invocation_service, invocation_context_provider):
    """Bind dependencies to one async execution context, including child tasks."""
    token = _binding.set(ManagedA2ABinding(invocation_service, invocation_context_provider))
    try:
        yield
    finally:
        _binding.reset(token)


class ManagedA2AEvent(Event):
    """Native projection carrying a private reference to the already-stored fact."""

    _canonical: Any = PrivateAttr(default=None)


def canonical_event_from_adk(event):
    return event._canonical if isinstance(event, ManagedA2AEvent) else None


class A2ARemoteAgent(BaseAgent):
    _remote_spec: RemoteAgentSpec = PrivateAttr()
    _invocation_service: Any = PrivateAttr(default=None)
    _invocation_context_provider: Any = PrivateAttr(default=None)

    def __init__(self, *, spec, invocation_service=None, invocation_context_provider=None):
        super().__init__(name=spec.local_name, description=spec.description or "")
        self._remote_spec = spec
        self._invocation_service = invocation_service
        self._invocation_context_provider = invocation_context_provider

    @property
    def remote_spec(self):
        return self._remote_spec

    async def _run_async_impl(self, ctx):
        binding = _binding.get()
        service = self._invocation_service or (binding.invocation_service if binding else None)
        provider = self._invocation_context_provider or (
            binding.invocation_context_provider if binding else None
        )
        if provider is None:
            raise RuntimeError("A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED")
        resolved = provider(ctx, self._remote_spec)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        from ksadk.a2a.adk_invocation_context import ManagedADKInvocationContext

        prefix_events = ()
        if isinstance(resolved, ManagedADKInvocationContext):
            request, trigger, service = resolved.request, resolved.trigger, resolved.service
            prefix_events = resolved.prefix_events
        else:
            request, trigger = resolved
        if service is None:
            raise RuntimeError("A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED")
        if not isinstance(request, A2AInvocationRequest) or not isinstance(
            trigger, ScopeTriggerRef
        ):
            raise TypeError("A2A_MANAGED_INVOCATION_CONTEXT_INVALID")
        if request.spec != self._remote_spec:
            raise ValueError("A2A_BINDING_MISMATCH")
        for event in prefix_events:
            if event.run_id != request.handle.run_id or event.scope_id != trigger.scope_id:
                raise ValueError("A2A_TRIGGER_REF_CONFLICT")
            yield _project(event, self.name, ctx)
        try:
            from ksadk.a2a.runtime_input import invoke_with_input

            async with aclosing(
                invoke_with_input(service, request, trigger_ref=trigger)
            ) as events:
                async for canonical in events:
                    yield _project(canonical, self.name, ctx)
        except (asyncio.CancelledError, TimeoutError):
            # Persist an honest cancel request; unsupported/unknown is not a
            # cancelled child. The runtime owns durable follow-up scheduling.
            try:
                handle = service.handle(request.scope_id)
                await asyncio.shield(service.cancel(handle))
            finally:
                raise


def _project(canonical, author, ctx):
    # ADK excludes partial deliveries from session history. Interaction events
    # carry live waiters/Futures for the Kernel bridge, not native model content;
    # persisting them makes the next session deepcopy fail. Their durable facts
    # remain in the Kernel interaction log and the original object still reaches
    # the canonical adapter for live approval/input delivery.
    partial = isinstance(canonical, (ItemUpdated, InteractionRequested))
    parts = []
    if isinstance(canonical, ItemUpdated) and isinstance(canonical.update, TextContent):
        parts = [types.Part(text=canonical.update.text)]
    elif isinstance(canonical, (ItemCompleted, ItemSnapshotReplaced)):
        parts = [
            types.Part(text=p.text) for p in canonical.snapshot.parts if isinstance(p, TextContent)
        ]
    native = ManagedA2AEvent(
        # ADK persists every delivery, including recovery projections, with
        # a unique native row ID. The stable public fact identity lives in
        # _canonical/custom_metadata; reusing it here breaks native resume.
        invocation_id=ctx.invocation_id,
        author=author,
        branch=ctx.branch,
        partial=partial,
        content=types.Content(role="model", parts=parts) if parts else None,
        custom_metadata={
            "ksadk_a2a_canonical_ref": {
                "event_id": canonical.event_id,
                "run_id": canonical.run_id,
                "scope_id": canonical.scope_id,
                "item_id": getattr(canonical, "item_id", None),
            }
        },
    )
    native._canonical = canonical
    return native


def remote_agent(
    spec: RemoteAgentSpec | None = None,
    *,
    invocation_service=None,
    invocation_context_provider=None,
    exposure_plan=None,
    **fields,
):
    """Construct an ADK BaseAgent without discovery or AgentCard network I/O."""
    if spec is not None and fields:
        raise ValueError("provide spec or named declaration fields")
    spec = spec or RemoteAgentSpec(**fields)
    plan = exposure_plan or compile_exposure_plan(
        [spec], framework="adk", consumer_id=spec.local_name
    )
    matches = [entry for entry in plan.entries if entry.spec.binding_id == spec.binding_id]
    if (
        plan.framework != "adk"
        or len(matches) != 1
        or matches[0].spec != spec
        or matches[0].exposure != "native"
    ):
        raise ValueError("A2A_EXPOSURE_CONFLICT")
    return A2ARemoteAgent(
        spec=spec,
        invocation_service=invocation_service,
        invocation_context_provider=invocation_context_provider,
    )


def handoff_tool(spec: RemoteAgentSpec):
    """Create a fixed-target ADK tool backed by the shared invocation service."""
    from ksadk.a2a.adk_tools import ManagedA2AHandoffTool
    return ManagedA2AHandoffTool(spec)
