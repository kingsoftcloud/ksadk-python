"""Merge native Codex facts and fixed-expert facts under Kernel ownership."""
import asyncio
from contextlib import aclosing
from dataclasses import replace

from ksadk.a2a.codex import CodexInvocationContextProvider, CodexToolCall
from ksadk.codex.a2a_tools import denied_tool_result, failed_tool_result
from ksadk.events.run_authority import current_run_event_authority


class CodexRecoveryCancelled(RuntimeError):
    pass


async def managed_codex_events(client, bootstrap, native_events, *, request, owner=None, recovery=None):
    authority = current_run_event_authority()
    if authority is None:
        raise RuntimeError('A2A_KERNEL_AUTHORITY_REQUIRED')
    if not bootstrap.outbound_enabled:
        raise RuntimeError('A2A_OUTBOUND_DISABLED')
    bridge = client._a2a_expert_bridge
    services = {}
    authority.register_managed_a2a(bootstrap)
    policy = authority.managed_invocation_policy()

    def service_for_space(space):
        if space not in services:
            recovery_options = {"recover_checkpoint_leases": True} if recovery is not None else {}
            services[space] = bootstrap.invocation_service_for_space(
                space, event_store=authority, policy=policy, **recovery_options)
        return services[space]

    provider = CodexInvocationContextProvider(authority=authority,
        service_for_space=service_for_space, caller_agent_id=bootstrap.runtime_metadata.agent_id,
        require_durable=recovery is not None)
    output = asyncio.Queue(maxsize=1)
    persisted = asyncio.Condition()
    triggers = set()
    calls = {}
    from ksadk.a2a.codex_checkpoint import CodexCheckpointUnavailable, CodexCheckpointWriter

    checkpoints = CodexCheckpointWriter(
        client=client, authority=authority, request=request, experts=bridge.experts, recovery=recovery,
    )

    async def cancel_experts():
        pending = [task for task in calls.values() if not task.done()]
        for call_task in pending:
            call_task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    if owner is not None:
        owner.cancel_experts = cancel_experts

    async def emit(event):
        ack = asyncio.Event()
        await output.put((event, ack))
        await ack.wait()

    async def invoke(call):
        try:
            # A server request may win the race with its earlier native item
            # notification. Continue draining native events until Kernel has
            # consumed that trigger; provider then verifies the persisted fact.
            async with persisted:
                await persisted.wait_for(lambda: bridge.key(call) in triggers)
            response = await bridge.experts[call.tool].invoke(
                call, provider=provider, emit=emit, before_invoke=checkpoints.before_invoke,
            )
        except asyncio.CancelledError:
            client.resolve_a2a_tool(call, failed_tool_result('A2A_CODEX_TOOL_CANCELED'))
            raise
        except TimeoutError:
            # Settle the parent as failed instead of returning a retryable tool
            # result to the model, which could create another remote task.
            await output.put((RuntimeError('A2A_CODEX_EXPERT_TIMEOUT'), None))
            return
        except CodexCheckpointUnavailable as exc:
            # Never turn an uncertain private write into a model-retryable tool error.
            await output.put((exc, None))
            return
        except PermissionError as exc:
            if str(exc) in {'A2A_APPROVAL_DENIED', 'A2A_INPUT_REJECTED'}:
                bridge.mark_denied(call)
                response = denied_tool_result()
            else:
                response = failed_tool_result('A2A_CODEX_EXPERT_FAILED')
        except Exception:
            response = failed_tool_result('A2A_CODEX_EXPERT_FAILED')
        client.resolve_a2a_tool(call, response)

    async def recover_call(call):
        # The provider finds the original request/attempt from the same durable
        # canonical trigger; invoke_with_input rejoins that service checkpoint.
        try:
            response = await bridge.experts[call.tool].invoke(
                call, provider=provider, emit=emit, before_invoke=checkpoints.before_invoke,
            )
        except TimeoutError:
            raise RuntimeError("A2A_CODEX_EXPERT_TIMEOUT") from None
        items = response.get("contentItems", [])
        if (len(items) != 1 or items[0].get("type") != "inputText"
                or not isinstance(items[0].get("text"), str)):
            raise ValueError("A2A_CODEX_RECOVERY_OUTPUT_INVALID")
        # Native dynamic tools persist the text content, not their RPC envelope.
        # Match that representation for calls already resolved before the crash.
        try:
            rollout = await client.ensure_managed_a2a_output(call, items[0]["text"])
        except TimeoutError:
            # This run's uncertain injection must not close the app-owned
            # transport and interrupt unrelated threads.
            raise RuntimeError("A2A_CODEX_RECOVERY_OUTPUT_UNCERTAIN") from None
        # Persist the flushed output before any new native turn may start.
        await checkpoints.before_invoke(call)
        from ksadk.a2a.codex_recovery_events import recovered_expert_terminal

        terminal = await recovered_expert_terminal(authority, call, rollout, response)
        if terminal is not None:
            await emit(terminal)

    async def pump():
        try:
            if recovery is not None:
                for call in recovery.calls:
                    calls[bridge.key(call)] = asyncio.create_task(recover_call(call))
                await asyncio.gather(*calls.values())
                await checkpoints.mark_continuation_started()
            async with aclosing(native_events):
                async for event in native_events:
                    if isinstance(event, dict):
                        call = CodexToolCall.model_validate(event['params'])
                        key = bridge.key(call)
                        if event['method'] == 'ksadk/a2a/toolCall':
                            if key not in calls:
                                calls[key] = asyncio.create_task(invoke(call))
                        elif event['method'] == 'ksadk/a2a/toolCancel':
                            await cancel_experts()
                            raise RuntimeError('A2A_CODEX_EXPERT_TIMEOUT')
                        else:
                            raise ValueError('A2A_CODEX_INTERNAL_EVENT_INVALID')
                        continue
                    if event.event_type == 'run.completed' and event.source.framework == 'codex':
                        await checkpoints.completed_turn(event)
                    await emit(event)
                    source = event.source
                    if getattr(event, 'item_kind', None) == 'tool_call' and source.framework == 'codex':
                        async with persisted:
                            triggers.add((source.metadata.get('thread_id'), source.native_run_id, source.native_item_id))
                            persisted.notify_all()
            await output.put((None, None))
        except asyncio.CancelledError:
            # A canceled recovery child can finish before any native turn
            # exists. Wake the consumer; ordinary generator teardown must not
            # enqueue into a queue whose consumer has already gone away.
            if recovery is not None and not asyncio.current_task().cancelling():
                await output.put((CodexRecoveryCancelled(), None))
            else:
                raise
        except Exception as exc:
            await output.put((exc, None))

    from ksadk.runtime_context import (
        TRUSTED_IDENTITY_METADATA_KEY, get_current_invocation_context,
        platform_invocation_scope, session_invocation_context,
    )
    context = get_current_invocation_context() or session_invocation_context(
        request.metadata.get("session_context"), agent_id=bootstrap.runtime_metadata.agent_id,
        user_id=request.user_id, session_id=request.session_id, runner_type="codex",
        identity=request.metadata.get(TRUSTED_IDENTITY_METADATA_KEY),
    )
    profile = request.metadata.get("tool_approval_mode")
    if isinstance(profile, str) and profile in {"ask", "risk", "full"}:
        # Kernel's per-run choice must win over an environment default or a
        # surrounding invocation. Keep the caller's context object unchanged.
        context = replace(context, tool_approval_mode=profile)
    with platform_invocation_scope(context):
        task = asyncio.create_task(pump())
    try:
        while True:
            event, ack = await output.get()
            if event is None:
                break
            if isinstance(event, Exception):
                if isinstance(event, CodexCheckpointUnavailable):
                    await cancel_experts()
                    await client.interrupt_active_turn(event.thread_id)
                raise event
            try:
                yield event
            finally:
                ack.set()
    finally:
        if owner is not None:
            owner.cancel_experts = None
        task.cancel()
        for call_task in calls.values():
            call_task.cancel()
        await asyncio.gather(task, *calls.values(), return_exceptions=True)
