"""Run-local managed A2A dependencies for the production ADK canonical stream."""

import asyncio
from contextlib import aclosing, suppress

from ksadk.a2a.adk import bind_managed_a2a
from ksadk.a2a.adk_invocation_context import ADKInvocationContextProvider
from ksadk.events.run_authority import current_run_event_authority


def managed_message_parts(ctx, _spec):
    """Forward visible text context, excluding thoughts, tool payloads and state."""
    parts = []
    branch = getattr(ctx, "branch", None) or ""
    for event in ctx.session.events:
        event_branch = getattr(event, "branch", None) or ""
        if event_branch and branch != event_branch and not branch.startswith(event_branch + "."):
            continue
        if getattr(event, "partial", False):
            continue
        text = "".join(
            part.text
            for part in (getattr(event.content, "parts", None) or ())
            if part.text and not getattr(part, "thought", False)
        )
        if text.strip():
            parts.append(f"[{event.author}]\n{text}")
    if not parts:
        parts = [
            part.text
            for part in (getattr(getattr(ctx, "user_content", None), "parts", None) or ())
            if part.text and not getattr(part, "thought", False)
        ]
    return tuple(parts)


async def managed_native_events(runner, native_events):
    bootstrap = getattr(runner, "_managed_a2a_bootstrap", None)
    authority = current_run_event_authority()
    from ksadk.sessions import bind_session_service, resolve_session_service

    session_service = resolve_session_service()
    async with aclosing(native_events):
        if bootstrap is None or not bootstrap.outbound_enabled or authority is None:
            while True:
                try:
                    # Pull the native generator while the Kernel-owned service
                    # is explicitly active.  Async-generator resumption may
                    # otherwise occur in a context that lost the worker binding.
                    with bind_session_service(session_service):
                        event = await anext(native_events)
                except StopAsyncIteration:
                    break
                yield event
            return
        authority.register_managed_a2a(bootstrap)
        policy = authority.managed_invocation_policy()
        services = {}

        def service_for_space(space_id):
            if space_id not in services:
                services[space_id] = bootstrap.invocation_service_for_space(
                    space_id, event_store=authority, policy=policy
                )
            return services[space_id]

        provider = ADKInvocationContextProvider(
            authority=authority,
            service_for_space=service_for_space,
            caller_agent_id=bootstrap.runtime_metadata.agent_id,
            message_selector=managed_message_parts,
        )
        from ksadk.a2a.adk_tools import (
            ManagedADKTools,
            bind_managed_adk_tools,
            install_managed_tools,
        )

        queue = asyncio.Queue(maxsize=1)
        # The native ADK iterator is consumed in a child task.  Capture the
        # Kernel-bound service before that task is created and bind it explicitly
        # inside the pump; relying on an implicit ContextVar copy is unsafe when
        # async-generator finalization moves across task contexts.
        async def emit(event):
            acknowledged = asyncio.Event()
            await queue.put((event, acknowledged))
            await acknowledged.wait()

        async def pump():
            with bind_session_service(session_service):
                try:
                    async for event in native_events:
                        await emit(event)
                except asyncio.CancelledError as exc:
                    if asyncio.current_task().cancelling():
                        raise
                    await queue.put((exc, None))
                except Exception as exc:
                    await queue.put((exc, None))
                else:
                    await queue.put((None, None))

        install_managed_tools(runner._agent)
        runtime = ManagedADKTools(
            bootstrap=bootstrap,
            authority=authority,
            services=service_for_space,
            emit=emit,
        )
        with bind_managed_a2a(None, provider), bind_managed_adk_tools(runtime):
            task = asyncio.create_task(pump())
            try:
                while True:
                    event, acknowledged = await queue.get()
                    if event is None:
                        break
                    if isinstance(event, BaseException):
                        raise event
                    try:
                        yield event
                    finally:
                        acknowledged.set()
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
