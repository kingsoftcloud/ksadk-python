"""Cancellation and timeout control around one prepared Native effect."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress

from ksadk.harness.stores.native_contract import RunStore

from .child_lifecycle import ChildLifecyclePort
from .context_contributions import ContextPort
from .effect_admission import effect_admission
from .effect_runtime import dispatch_effect
from .hook_runtime_contract import HookPort
from .lease import RunLeaseController
from .model_runtime import StreamFlushError
from .ports import (
    ArtifactPort,
    ModelPort,
    RunConfiguration,
    ToolPort,
    VerificationPort,
    WorkspacePort,
)
from .state import EffectIntent, EffectReceipt, RunScope, RunSnapshot

_CANCELLATION_JOIN_SECONDS = 0.25


def _discard_late_effect_result(task: asyncio.Task) -> None:
    """Drain a detached port result without exposing exception content."""
    with suppress(asyncio.CancelledError, Exception):
        task.exception()


class NativeEffectDispatcher:
    """Bind host ports once, then keep the worker lease alive during effects."""

    def __init__(
        self,
        store: RunStore,
        scope: RunScope,
        leases: RunLeaseController,
        model: ModelPort,
        tools: ToolPort | None,
        verifier: VerificationPort | None,
        workspace: WorkspacePort | None,
        artifacts: ArtifactPort | None,
        context_port: ContextPort | None = None,
        hook_port: HookPort | None = None,
        child_lifecycle_port: ChildLifecyclePort | None = None,
    ) -> None:
        self.store, self.scope, self.leases = store, scope, leases
        self.model, self.tools, self.verifier = model, tools, verifier
        self.workspace, self.artifacts = workspace, artifacts
        self.context_port = context_port
        self.hook_port = hook_port
        self.child_lifecycle_port = child_lifecycle_port

    async def dispatch(
        self,
        snapshot: RunSnapshot,
        intent: EffectIntent,
        config: RunConfiguration,
        *,
        wake: asyncio.Event,
        emit,
        flush_callback: Callable[[], None] | None = None,
        flush_interval: float = 0.2,
        on_child_promotion=None,
        on_child_input=None,
        on_child_cancellation=None,
        on_parallel_child=None,
        remaining_wall_seconds: float | None = None,
        reconcile_tool: bool = False,
    ) -> EffectReceipt:
        return await self.leases.keep_alive(
            snapshot.run_id,
            dispatch_cancellable_effect(
                snapshot,
                intent,
                config,
                model=self.model,
                tools=self.tools,
                verifier=self.verifier,
                workspace=self.workspace,
                artifacts=self.artifacts,
                # A child-cancel effect exists to settle the already queued
                # parent cancellation. That same command cannot abort cleanup.
                pending_commands=lambda: (
                    () if intent.kind == "child"
                    and intent.payload["request"]["action"] == "cancel"
                    else self.store.commands(self.scope, snapshot.run_id)
                ),
                wake=wake,
                emit=emit,
                flush_callback=flush_callback,
                flush_interval=flush_interval,
                on_child_promotion=on_child_promotion,
                on_child_input=on_child_input,
                on_child_cancellation=on_child_cancellation,
                on_parallel_child=on_parallel_child,
                remaining_wall_seconds=(None if intent.kind == "child"
                                        and intent.payload["request"]["action"] == "cancel"
                                        else remaining_wall_seconds),
                reconcile_tool=reconcile_tool,
                context_port=self.context_port,
                hook_port=self.hook_port,
                child_lifecycle_port=self.child_lifecycle_port,
            ),
        )


async def dispatch_cancellable_effect(
    snapshot: RunSnapshot,
    intent: EffectIntent,
    config: RunConfiguration,
    *,
    model: ModelPort,
    tools: ToolPort | None,
    verifier: VerificationPort | None,
    workspace: WorkspacePort | None,
    artifacts: ArtifactPort | None,
    pending_commands,
    wake: asyncio.Event,
    emit,
    flush_callback: Callable[[], None] | None = None,
    flush_interval: float = 0.2,
    on_child_promotion=None,
    on_child_input=None,
    on_child_cancellation=None,
    on_parallel_child=None,
    remaining_wall_seconds: float | None = None,
    reconcile_tool: bool = False,
    context_port: ContextPort | None = None,
    hook_port: HookPort | None = None,
    child_lifecycle_port: ChildLifecyclePort | None = None,
) -> EffectReceipt:
    stream_open = True
    loop = asyncio.get_running_loop()
    wall_bounded = (
        remaining_wall_seconds is not None
        and remaining_wall_seconds <= config.limits.effect_timeout_seconds
    )
    timeout_seconds = (
        max(0, remaining_wall_seconds) if wall_bounded else config.limits.effect_timeout_seconds
    )
    deadline = loop.time() + timeout_seconds
    timeout_reason = "wall_time_hard_limit" if wall_bounded else "effect_timeout"
    idle_model = (
        intent.kind == "model" and config.profile.streaming
        and config.profile.stream_timeout_policy == "idle/v1"
    )
    wall_deadline = (
        loop.time() + max(0, remaining_wall_seconds)
        if remaining_wall_seconds is not None else float("inf")
    )

    def emit_while_open(event) -> None:
        nonlocal deadline, timeout_reason, wall_bounded
        # A port that ignores cancellation may still emit after the uncertain
        # receipt has closed this attempt. Late deltas must not mutate the Run.
        if stream_open:
            if idle_model:
                now = loop.time()
                # Progress cannot revive an expired lease or extend the Run's wall limit.
                if now < deadline:
                    idle_deadline = now + config.limits.effect_timeout_seconds
                    deadline = min(idle_deadline, wall_deadline)
                    wall_bounded = wall_deadline <= idle_deadline
                    timeout_reason = "wall_time_hard_limit" if wall_bounded else "effect_timeout"
                    admission.deadline = deadline
            # Activity proves liveness only; it is never a transcript or UI fact.
            if event.kind != "activity":
                emit(event)

    async def child_while_open(index, raw_call, operation_key, result) -> None:
        if stream_open and on_parallel_child is not None:
            await on_parallel_child(index, raw_call, operation_key, result)

    def revocation_requested() -> bool:
        commands = pending_commands()
        return bool(commands and commands[0].kind in {"cancel", "policy_revoked"})

    with effect_admission(
        deadline=deadline, clock=loop.time, revocation_requested=revocation_requested,
    ) as admission:
        task = asyncio.create_task(
            dispatch_effect(
                snapshot,
                intent,
                config,
                model=model,
                tools=tools,
                verifier=verifier,
                workspace=workspace,
                artifacts=artifacts,
                emit=emit_while_open,
                flush_callback=flush_callback,
                flush_interval=flush_interval,
                on_parallel_child=child_while_open if on_parallel_child is not None else None,
                reconcile_tool=reconcile_tool,
                context_port=context_port,
                hook_port=hook_port,
                child_lifecycle_port=child_lifecycle_port,
            )
        )
    cleanup_started = False

    async def stop_unfinished() -> bool:
        nonlocal stream_open, cleanup_started
        stream_open = False
        # Task.cancel() can be swallowed or uncancelled by an authorizer.
        # Remove dispatch authority before asking the owned Task to stop.
        admission.revoke()
        if task.done():
            return True
        if not cleanup_started:
            cleanup_started = True
            task.add_done_callback(_discard_late_effect_result)
            task.cancel()
        await asyncio.wait((task,), timeout=_CANCELLATION_JOIN_SECONDS)
        return task.done()

    try:
        while not task.done():
            commands = pending_commands()
            if (commands and commands[0].kind == "cancel_child"
                    and on_child_cancellation is not None):
                await on_child_cancellation(commands[0])
                continue
            if commands and commands[0].kind in {"cancel", "policy_revoked"}:
                await stop_unfinished()
                return EffectReceipt(
                    operation_key=intent.operation_key,
                    status="uncertain",
                    result={"reason": "control_interrupted_effect", "resource_usage_unknown": True},
                )
            if commands and commands[0].kind == "promote_child" and on_child_promotion is not None:
                # Delivery control never cancels or replays the in-flight tool.
                # Consume one command atomically before releasing its waiter.
                on_child_promotion(commands[0])
                continue
            if commands and commands[0].kind == "message_child" and on_child_input is not None:
                on_child_input(commands[0])
                continue
            if commands and commands[0].kind == "pause" and intent.kind == "model":
                # A model exchange has not dispatched a tool. Join its local
                # cancellation before accounting it as an unsealed attempt;
                # the pending pause command then suspends the same run.
                joined = await stop_unfinished()
                return EffectReceipt(
                    operation_key=intent.operation_key,
                    status="failed" if joined else "uncertain",
                    result={
                        "reason": (
                            "model_paused_before_result" if joined else "effect_outcome_unknown"
                        )
                    },
                )
            if (commands and commands[0].kind == "pause" and intent.kind == "child"
                    and config.child_lifecycle_policy == "owned-background/v2"
                    and intent.payload["request"]["action"] in {"poll", "wait"}):
                # Cancel this read-only waiter, not the independently owned
                # child. Only a joined observation is safe to resume later.
                joined = await stop_unfinished()
                return EffectReceipt(
                    operation_key=intent.operation_key,
                    status="failed" if joined else "uncertain",
                    result={"reason": "child_observation_paused" if joined
                            else "effect_outcome_unknown"},
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                joined = await stop_unfinished()
                return EffectReceipt(
                    operation_key=intent.operation_key,
                    status="uncertain",
                    result={"reason": timeout_reason if joined else "effect_outcome_unknown",
                            **({"deadline_cause": timeout_reason} if wall_bounded else {})},
                )
            waiter = asyncio.create_task(wake.wait())
            try:
                await asyncio.wait(
                    (task, waiter),
                    timeout=min(remaining, 0.1),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                waiter.cancel()
                # Consume only the wake waiter's own cancellation. An outer
                # driver cancellation must reach the effect cleanup below.
                await asyncio.gather(waiter, return_exceptions=True)
                wake.clear()
        # A blocking adapter can starve the timeout waiter. The task may be
        # done when control returns even though its result arrived after the
        # pinned effect deadline. Never seal that late result as on-time.
        if loop.time() >= deadline:
            if not task.cancelled():
                with suppress(Exception):
                    task.exception()
            return EffectReceipt(
                operation_key=intent.operation_key,
                status="uncertain",
                result={"reason": timeout_reason,
                        **({"deadline_cause": timeout_reason} if wall_bounded else {})},
            )
        if task.cancelled():
            return EffectReceipt(
                operation_key=intent.operation_key,
                status="uncertain",
                result={"reason": "effect_child_canceled"},
            )
        # Check for local persistence failures (flush_callback CAS/fence/store
        # errors) BEFORE the generic except. These must not be masked by a
        # generic "effect_port_error" uncertain receipt. A StreamFlushError
        # indicates the host's own store commit failed — the caller must see
        # the original cause, not a sanitized port error.
        exc = task.exception()
        if exc is not None and isinstance(exc, StreamFlushError):
            raise exc
        result = task.result()
        if reconcile_tool and result.status not in {"ok", "error"}:
            return EffectReceipt(
                operation_key=intent.operation_key,
                status="uncertain",
                result={
                    "reason": "effect_outcome_unknown",
                    "reconciliation_result": result.model_dump(mode="json"),
                },
            )
        return EffectReceipt(
            operation_key=intent.operation_key,
            status="committed",
            result=result.model_dump(mode="json"),
        )
    except StreamFlushError:
        # Local persistence failure (CAS/fence/store from flush_callback).
        # Propagate the original cause; do not mask with a generic uncertain
        # receipt. The caller (_commit_receipt) will handle this as a store
        # conflict, not an unknown port outcome.
        raise
    except Exception:
        # Provider/tool exception bodies may contain secrets. A port owns safe
        # structured errors; unknown exceptions cannot prove execution.
        return EffectReceipt(
            operation_key=intent.operation_key,
            status="uncertain",
            result={"reason": "effect_port_error"},
        )
    finally:
        admission.revoke()
        stream_open = False
        if not task.done() and not cleanup_started:
            # The receipt remains uncertain if the port ignores cancellation.
            # Joining indefinitely would defeat the effect deadline and block
            # the Run. A late tool effect stays uncertain until reconciled.
            await stop_unfinished()


__all__ = ["dispatch_cancellable_effect"]
