"""Lease takeover for a committed, undispatched child effect.

Verify-only recovery remains separate from ordinary Kernel continuation.
Both grants retain the original configuration, origin and lease policy.
"""

from __future__ import annotations

import asyncio

from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.state import RunPhase, RunSnapshot
from ksadk.harness.stores.native_contract import LeaseBusy


def _prepared(
    kernel: NativeKernel, snapshot: RunSnapshot, original: RunSnapshot, *,
    allow_execution: bool = False, allow_compaction: bool = False,
) -> bool:
    intent = kernel.store.get_intent(kernel.scope, snapshot.run_id, snapshot.pending_intent)
    phases = {"verify": RunPhase.VERIFYING}
    if allow_execution:
        phases.update(model=RunPhase.REQUESTING_MODEL, tool=RunPhase.EXECUTING_TOOLS)
    phase_allowed = intent is not None and snapshot.phase == phases.get(intent.kind)
    if (
        allow_execution and allow_compaction and intent is not None
        and intent.kind == "model" and snapshot.phase == RunPhase.COMPACTING
    ):
        # This permission belongs only to the checkpoint grant. Verify-only
        # and the older prepared-execution grant retain their original scope.
        phase_allowed = (
            intent.payload == {"purpose": "compaction"}
            and snapshot.compaction_request is not None
            and kernel.context.planner is not None
            and snapshot.context_binding == kernel.context.binding
        )
    return (
        phase_allowed
        and snapshot.goal_revision == original.goal_revision == 0
        and snapshot.goal == original.goal
        and snapshot.origin == original.origin
        and snapshot.session_id == original.session_id
        and snapshot.pending_intent == original.pending_intent
        and intent.status == "prepared"
        and (intent.kind != "tool" or isinstance(intent.payload.get("call"), dict))
        and kernel.store.get_receipt(kernel.scope, snapshot.run_id, intent.operation_key) is None
        and not kernel.store.commands(kernel.scope, snapshot.run_id)
    )


async def _claim_prepared(
    kernel: NativeKernel, original: RunSnapshot, *, ttl: float | None, timeout: float,
    allow_execution: bool,
) -> tuple[RunSnapshot, bool]:
    if (
        ttl is None or original.origin.get("lease_policy") != "native.owner_lease/v1"
        or type(original.origin.get("lease_ttl_seconds")) not in {int, float}
        or original.origin["lease_ttl_seconds"] != ttl
        or not _prepared(kernel, original, original, allow_execution=allow_execution)
    ):
        raise ValueError("child_prepared_recovery_rejected")
    deadline = asyncio.get_running_loop().time() + min(timeout, ttl + 1)
    while True:
        current = kernel.snapshot(original.run_id)
        if current.phase.terminal:
            return current, False
        if not _prepared(kernel, current, original, allow_execution=allow_execution):
            raise ValueError("child_prepared_recovery_rejected")
        try:
            kernel.attach(original.run_id)
        except LeaseBusy:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ValueError("child_owner_still_active") from None
            await asyncio.sleep(min(0.05, remaining))
        else:
            # The old owner could commit between our read and lease acquisition.
            # Return ownership before any await so the caller always releases it.
            try:
                return kernel.snapshot(original.run_id), True
            except BaseException:
                kernel.release(original.run_id)
                raise


async def claim_prepared_verification(
    kernel: NativeKernel, original: RunSnapshot, *, ttl: float | None, timeout: float
) -> tuple[RunSnapshot, bool]:
    return await _claim_prepared(kernel, original, ttl=ttl, timeout=timeout,
                                 allow_execution=False)


async def claim_prepared_execution(
    kernel: NativeKernel, original: RunSnapshot, *, ttl: float | None, timeout: float
) -> tuple[RunSnapshot, bool]:
    return await _claim_prepared(kernel, original, ttl=ttl, timeout=timeout,
                                 allow_execution=True)


async def continue_prepared_execution(
    kernel: NativeKernel, snapshot: RunSnapshot, original: RunSnapshot
) -> RunSnapshot:
    if not _prepared(kernel, snapshot, original, allow_execution=True):
        return snapshot
    # The ordinary Kernel still owns admission, remaining budgets, commands,
    # receipts and verification. No host executor restarts unknown effects.
    return await kernel.run_until_idle(snapshot.run_id)


async def finish_prepared_verification(
    kernel: NativeKernel, snapshot: RunSnapshot, original: RunSnapshot
) -> RunSnapshot:
    if not _prepared(kernel, snapshot, original):
        return snapshot
    snapshot = await kernel.advance(snapshot.run_id)
    # A failed verifier can schedule repair. That is outside this recovery grant.
    # Finalization is a local commit; a new model/tool/verification is forbidden.
    if snapshot.phase == RunPhase.FINALIZING and not snapshot.pending_intent:
        snapshot = await kernel.advance(snapshot.run_id)
    return snapshot
