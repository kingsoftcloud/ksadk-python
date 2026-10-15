"""Keep persisted conversations with the engine that owns their history."""

from ksadk.studio.contracts import RunStatus


def native_session_enabled(event_store, agent_id: str, session_id: str, *, selected: bool) -> bool:
    for record in event_store.list_runs(session_id=session_id, agent_id=agent_id):
        handle = record.runtime_handle
        # An admission failure without a runtime handle has no engine history.
        # Completed older records still belong to Managed even without a handle.
        if record.status != RunStatus.COMPLETED and not handle.get("run_id"):
            continue
        return (handle.get("native_ref") or {}).get("engine_kind") == "native"
    return selected
