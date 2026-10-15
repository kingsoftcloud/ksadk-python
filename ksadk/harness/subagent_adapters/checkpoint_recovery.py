"""An explicit grant for committed child checkpoints and undispatched effects.

This host adapter never starts a missing child or replays an unknown operation.
The ordinary Kernel remains responsible for all subsequent effects and budgets.
"""

from __future__ import annotations

import asyncio

from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.state import RunPhase, RunSnapshot
from ksadk.harness.stores.native_contract import LeaseBusy

from .prepared_recovery import _prepared

_LOCAL_PHASES = frozenset(
    {
        RunPhase.CREATED,
        RunPhase.PREPARING_CONTEXT,
        RunPhase.REQUESTING_MODEL,
        RunPhase.EXECUTING_TOOLS,
        RunPhase.COMPACTING,
        RunPhase.VERIFYING,
        RunPhase.FINALIZING,
    }
)


def _checkpoint(kernel: NativeKernel, snapshot: RunSnapshot, original: RunSnapshot) -> bool:
    # Ownership can advance the head while a recovery waits. Revalidate its
    # immutable child identity and the complete receipt boundary after CAS;
    # a newer safe local commit is not permission to replay a claimed effect.
    if (
        snapshot.scope != original.scope
        or snapshot.run_id != original.run_id
        or snapshot.turn_id != original.turn_id
        or snapshot.session_id != original.session_id
        or snapshot.goal != original.goal
        or snapshot.origin != original.origin
        or snapshot.configuration_digest != original.configuration_digest
        or snapshot.context_binding != original.context_binding
        or snapshot.profile_digest != original.profile_digest
        or snapshot.contract_digest != original.contract_digest
        or kernel.store.commands(kernel.scope, snapshot.run_id)
    ):
        return False
    if snapshot.goal_revision != 0 or snapshot.phase not in _LOCAL_PHASES:
        return False
    if snapshot.pending_intent and not _prepared(
        kernel, snapshot, snapshot, allow_execution=True, allow_compaction=True
    ):
        return False
    if snapshot.pending_interaction or snapshot.interaction:
        return False
    # Every earlier dispatch must have a known original receipt. A prepared
    # current effect is the only exception; it has not acquired dispatch rights.
    for event in kernel.events(snapshot.run_id):
        if event.kind != "effect.prepared":
            continue
        key = event.payload.get("operation_key")
        if not isinstance(key, str) or not key:
            return False
        if key == snapshot.pending_intent:
            continue
        receipt = kernel.store.get_receipt(kernel.scope, snapshot.run_id, key)
        if (
            receipt is None
            or receipt.status == "uncertain"
            or receipt.result.get("status") == "uncertain"
        ):
            return False
    return True


async def claim_checkpoint_execution(
    kernel: NativeKernel,
    original: RunSnapshot,
    *,
    ttl: float | None,
    timeout: float,
) -> tuple[RunSnapshot, bool]:
    if (
        ttl is None
        or original.origin.get("lease_policy") != "native.owner_lease/v1"
        or type(original.origin.get("lease_ttl_seconds")) not in {int, float}
        or original.origin["lease_ttl_seconds"] != ttl
        or not _checkpoint(kernel, original, original)
    ):
        raise ValueError("child_checkpoint_recovery_rejected")
    deadline = asyncio.get_running_loop().time() + min(timeout, ttl + 1)
    while True:
        current = kernel.snapshot(original.run_id)
        if current.phase.terminal:
            return current, False
        if not _checkpoint(kernel, current, original):
            raise ValueError("child_checkpoint_recovery_rejected")
        try:
            kernel.attach(original.run_id)
        except LeaseBusy:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ValueError("child_owner_still_active") from None
            await asyncio.sleep(min(0.05, remaining))
        else:
            try:
                return kernel.snapshot(original.run_id), True
            except BaseException:
                kernel.release(original.run_id)
                raise


async def continue_checkpoint_execution(
    kernel: NativeKernel,
    snapshot: RunSnapshot,
    original: RunSnapshot,
) -> RunSnapshot:
    if not _checkpoint(kernel, snapshot, original):
        return snapshot
    return await kernel.run_until_idle(snapshot.run_id)
