"""Checkpoint publication before a managed LangGraph invocation can pause."""
import asyncio
import time

from ksadk.a2a.timeouts import seconds
from ksadk.events.canonical import ContinuationCreated, SourceRef
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.sessions.resilience import is_transient_session_backend_failure


async def managed_checkpoint(runner, authority, event):
    if event.event_type != 'interaction.requested' or not (
        getattr(event, '_invocation', None) is not None
        or getattr(event, '_managed_input_waiter', None) is not None
    ):
        return None
    capability = runner.describe_checkpoint_capability()
    if not (capability.get('Supported') and capability.get('SharedAcrossPods')):
        return None
    last_read_id = object()
    cap = seconds('KSADK_A2A_CHECKPOINT_READY_SECONDS', 45.0)
    invocation = getattr(event, '_invocation', None) or getattr(event, '_managed_input_waiter', None)
    deadline = getattr(invocation, 'deadline_unix_seconds', None)
    if deadline is not None:
        cap = min(cap, max(0.0, deadline - time.time()))
    # Pregel can start a tool while the preceding checkpoint's background
    # write is still finishing. Wait for publication before exposing approval;
    # never run the graph/tool again to obtain that checkpoint.
    try:
        async with asyncio.timeout(cap):
            config = await runner._get_session_config(authority.session_id)
            saver = getattr(getattr(runner, '_agent', None), 'checkpointer', None)
            peek = getattr(saver, 'apeek_latest_checkpoint_id', None)
            while True:
                if callable(peek):
                    try:
                        unchanged = await peek(config) == last_read_id
                    except Exception as exc:
                        if not is_transient_session_backend_failure(exc):
                            raise RuntimeError('A2A_NATIVE_RECOVERY_CHECKPOINT_REQUIRED') from exc
                        unchanged = True
                    if unchanged:
                        await asyncio.sleep(0.2)
                        continue
                state, error = await runner._read_graph_state(config)
                ref = runner._checkpoint_ref_from_state(state).get('langgraph', {})
                if error is None and ref.get('checkpoint_id') and getattr(state, 'next', ()):
                    break
                if error is not None and not is_transient_session_backend_failure(error):
                    raise RuntimeError('A2A_NATIVE_RECOVERY_CHECKPOINT_REQUIRED') from error
                if error is None:
                    if config.get('configurable', {}).get('checkpoint_id'):
                        raise RuntimeError('A2A_NATIVE_RECOVERY_CHECKPOINT_REQUIRED')
                    last_read_id = ref.get('checkpoint_id')
                await asyncio.sleep(0.2)
    except TimeoutError as exc:
        raise RuntimeError('A2A_NATIVE_RECOVERY_CHECKPOINT_REQUIRED') from exc
    scope = stable_scope_id('langgraph', authority.run_id, '$run')
    continuation = stable_item_id('langgraph', scope, 'managed-checkpoint', ref['checkpoint_id'])
    return ContinuationCreated(
        schema_version=2,
        event_id=stable_event_id('langgraph', scope, continuation, 'continuation.created',
                                'checkpoint', authority.publication_epoch, 0),
        seq=0, timestamp=event.timestamp, run_id=authority.run_id, scope_id=scope,
        source=SourceRef(framework='langgraph', native_run_id=authority.run_id,
                         metadata={'managed_a2a_takeover': True,
                                   'publication_epoch': authority.publication_epoch}),
        continuation_id=continuation, continuation_kind='graph_checkpoint',
        resumable=True, ref=ref,
    )
