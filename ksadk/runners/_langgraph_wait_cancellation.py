"""Retire an acknowledged native wait without resuming a business node."""

from __future__ import annotations

from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph

from ksadk.interaction.langgraph_observer import collect_native_waits

_CANCELLED_CHECKPOINT = "ksadk_cancelled_checkpoint"


async def retire_cancelled_wait(runner, handle) -> None:
    """Called only after the owner has persisted its canonical cancellation fact.

    LangGraph's public END update clears scheduled tasks, preserving completed
    parallel writes and all historical checkpoints. It does not answer an
    interrupt or execute a tool. Fresh-input recovery can subsequently close
    unmatched tool history without mistaking this for a still-live interrupt.
    """
    graph = runner._agent
    if not isinstance(graph, CompiledStateGraph):
        raise ValueError("Native wait cancellation requires CompiledStateGraph")
    ref = handle.native_ref
    expected = {key: str(ref.get(key) or "") for key in (
        "thread_id", "checkpoint_ns", "checkpoint_id",
    )}
    if not expected["thread_id"] or not expected["checkpoint_id"]:
        raise ValueError("Native wait cancellation requires its original checkpoint")
    config = {"configurable": {key: expected[key] for key in ("thread_id", "checkpoint_ns")}}
    snapshot, error = await runner._read_graph_state(config, include_subgraphs=True)
    if error is not None or snapshot is None:
        raise RuntimeError("Cannot read the native wait being cancelled") from error
    address = (snapshot.config or {}).get("configurable", {})
    if any(str(address.get(key) or "") != expected[key] for key in ("thread_id", "checkpoint_ns")):
        raise ValueError("Native wait cancellation identity mismatch")
    if (snapshot.metadata or {}).get(_CANCELLED_CHECKPOINT) == expected["checkpoint_id"]:
        if not snapshot.next and not collect_native_waits(snapshot):
            return  # Replay of the same acknowledged cancellation, not a new wait.
    if str(address.get("checkpoint_id") or "") != expected["checkpoint_id"]:
        raise ValueError("Native wait checkpoint changed before cancellation")
    actual = {(wait.task_id, wait.interrupt.native_id) for wait in collect_native_waits(snapshot)}
    targets = ref.get("native_waits") or {}
    recorded = {(str(target.get("task_id") or ""), str(target.get("call_id") or ""))
                for target in targets.values()}
    if not actual or actual != recorded:
        raise ValueError("Native wait cancellation does not match all suspended tasks")
    config["metadata"] = {_CANCELLED_CHECKPOINT: expected["checkpoint_id"]}
    await graph.aupdate_state(config, None, as_node=END)
