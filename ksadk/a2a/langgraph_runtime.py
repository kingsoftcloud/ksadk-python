"""Join LangGraph native events and managed A2A facts in one owned run stream."""

import asyncio
from contextlib import aclosing, suppress

from ksadk.a2a.langgraph_invocation_context import LangGraphInvocationContextProvider
from ksadk.a2a.langgraph_managed import bind_managed_langgraph
from ksadk.events.run_authority import current_run_event_authority


async def managed_langgraph_events(runner, native_events):
    bootstrap = getattr(runner, "_managed_a2a_bootstrap", None)
    async with aclosing(native_events):
        if bootstrap is None or not bootstrap.outbound_enabled:
            async for event in native_events:
                yield event
            return
        authority = current_run_event_authority()
        if authority is None:
            raise RuntimeError("A2A_KERNEL_AUTHORITY_REQUIRED")
        authority.register_managed_a2a(bootstrap)
        policy = authority.managed_invocation_policy()
        services = {}

        def service_for_space(space):
            if space not in services:
                services[space] = bootstrap.invocation_service_for_space(
                    space, event_store=authority, policy=policy,
                )
            return services[space]

        from ksadk.a2a.langgraph_tool_runtime import ManagedLangGraphTools, graph_exposure_plan

        runtime = ManagedLangGraphTools(
            bootstrap=bootstrap, authority=authority, plan=graph_exposure_plan(runner._agent),
        )
        provider = LangGraphInvocationContextProvider(
            authority=authority, service_for_space=service_for_space,
            caller_agent_id=bootstrap.runtime_metadata.agent_id,
            tool_trigger=runtime.tool_trigger,
        )
        from ksadk.sessions import bind_session_service, resolve_session_service

        session_service = resolve_session_service()
        queue = asyncio.Queue(maxsize=1)

        async def emit(event):
            from ksadk.a2a.langgraph_recovery import managed_checkpoint

            checkpoint = await managed_checkpoint(runner, authority, event)
            if checkpoint is not None:
                checkpoint_ack = asyncio.Event()
                await queue.put((checkpoint, checkpoint_ack))
                await checkpoint_ack.wait()
            acknowledged = asyncio.Event()
            await queue.put((event, acknowledged))
            await acknowledged.wait()

        async def pump():
            with bind_session_service(session_service), bind_managed_langgraph(None, provider, emit, runtime=runtime):
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
                    await runtime.notify_event()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
