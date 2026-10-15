"""Checkpoint publication before a managed LangGraph invocation can pause."""
from ksadk.events.canonical import ContinuationCreated, SourceRef
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id


async def managed_checkpoint(runner, authority, event):
    if event.event_type != 'interaction.requested' or not (
        getattr(event, '_invocation', None) is not None
        or getattr(event, '_managed_input_waiter', None) is not None
    ):
        return None
    capability = runner.describe_checkpoint_capability()
    if not (capability.get('Supported') and capability.get('SharedAcrossPods')):
        return None
    config = await runner._get_session_config(authority.session_id)
    state, error = await runner._read_graph_state(config)
    ref = runner._checkpoint_ref_from_state(state).get('langgraph', {})
    if error or not ref.get('checkpoint_id') or not getattr(state, 'next', ()):
        raise RuntimeError('A2A_NATIVE_RECOVERY_CHECKPOINT_REQUIRED')
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
