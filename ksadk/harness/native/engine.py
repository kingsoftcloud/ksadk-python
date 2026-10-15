"""Opt-in native kernel: schedule pure transitions and atomically commit effects.

This is the Phase 2 kernel, not the Studio ExecutionEngine facade. A durable
Store does not by itself establish recoverable external effects or cloud leases.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import suppress
from time import time
from uuid import uuid4

from ksadk.harness.stores.native_contract import (
    ForkingRunStore,
    PendingCommandConflict,
    RunStore,
    StoreConflict,
    check_scope,
)

from .activation_contract import ActivationResourceRef
from .child_interruption import admit_interrupted_child_receipts as admit_interrupted_child_receipts
from .child_lifecycle import (
    ChildLifecyclePort,
    prepare_child_lifecycle,
)
from .child_lifecycle import (
    ChildLifecycleRequest as ChildLifecycleRequest,
)
from .child_lifecycle import (
    ChildLifecycleResult as ChildLifecycleResult,
)
from .child_lifecycle import (
    recover_paused_child_observation as recover_paused_child_observation,
)
from .child_lifecycle import (
    reduce_child_lifecycle as reduce_child_lifecycle,
)
from .child_lifecycle_contract import (
    ChildInterruptedReceiptRequest as ChildInterruptedReceiptRequest,
)
from .child_notifications import child_ready_probe, stage_child_ready
from .command_runtime import resolve_command_batch, stage_child_cancellation
from .completion import TaskContract
from .context_contributions import ContextPort
from .context_contributions import reduce_collection as reduce_collection
from .context_contributor_contract import ContextModelDispatchDenial as ContextModelDispatchDenial
from .context_driver import ContextDriver
from .context_reduction import SUMMARY_REDUCTION_POLICY as SUMMARY_REDUCTION_POLICY
from .context_runtime import ContextPlanner
from .effect_control import NativeEffectDispatcher
from .effect_receipts import EffectReceiptCommitter
from .effective_configuration import effective_run_configuration
from .finalization_workspace import prepare_finalization_workspace
from .finalization_workspace import reduce_finalization_workspace as reduce_finalization_workspace
from .fork import ForkCreation, build_fork_snapshot, command_run_id
from .history import project_session_history
from .hook_runtime_contract import HookInvocationResult as HookInvocationResult
from .hook_runtime_contract import HookPort
from .lease import RunLeaseController
from .machine import (
    next_transition,
)
from .machine import (
    reduce_model as reduce_model,
)
from .machine import (
    reduce_parallel_readonly_tools as reduce_parallel_readonly_tools,
)
from .machine import (
    reduce_tool as reduce_tool,
)
from .machine import (
    reduce_workspace as reduce_workspace,
)
from .model import ModelTurnResult as ModelTurnResult
from .model_runtime import (
    provisional_delta as provisional_delta,
)
from .model_runtime import (
    recover_unsealed_model_receipt as recover_unsealed_model_receipt,
)
from .model_runtime import (
    retry_delay as retry_delay,
)
from .model_runtime import (
    sealed_model_usage_event as sealed_model_usage_event,
)
from .model_runtime import (
    unavailable_uncertain_usage as unavailable_uncertain_usage,
)
from .model_takeover import KEY as TAKEOVER_RESERVATION
from .model_takeover import ModelTakeoverCapability, prepare_reserved_model, settle_lost_model
from .ports import (
    ArtifactPort,
    BrowserRecoveryPort,
    ModelPort,
    RunConfiguration,
    ToolPort,
    VerificationPort,
    WorkspacePort,
    digest,
    validate_task_tool_scope,
)
from .ports import (
    ToolResult as ToolResult,
)
from .ports import (
    VerificationBundle as VerificationBundle,
)
from .ports import (
    WorkspaceObservation as WorkspaceObservation,
)
from .precompact_hook_runtime import prepare_precompact_hook
from .precompact_hook_runtime import reduce_precompact_hook as reduce_precompact_hook
from .request_media import ModelRequestDispatchDenial as ModelRequestDispatchDenial
from .run_clock import ActiveRunClock, wall_budget_stop
from .state import (
    CommandReceipt,
    CommitBatch,
    EffectReceipt,
    NativeEvent,
    RunCommand,
    RunPhase,
    RunScope,
    RunSnapshot,
)
from .state import (
    EffectIntent as EffectIntent,
)
from .stop_hook_runtime import prepare_stop_hook
from .stop_hook_runtime import reduce_stop_hook as reduce_stop_hook
from .tool_hook_runtime import (
    close_tool_hook_run as close_tool_hook_run,
)
from .tool_hook_runtime import (
    consume_hook_feedback as consume_hook_feedback,
)
from .tool_hook_runtime import (
    reduce_tool_hook as reduce_tool_hook,
)
from .tool_hook_runtime import (
    stage_post_hooks as stage_post_hooks,
)
from .transitions import commit_transition as commit_transition
from .transitions import finish as finish
from .user_input import NativeUserInput
from .verification_runtime import reduce_verification as reduce_verification


class NativeKernel(EffectReceiptCommitter):
    """One host-authorized scope; start/attach pins the complete configuration.

    Every side effect has a persisted intent before dispatch and one atomic
    receipt/state/event commit after dispatch. Lost running effects stop with
    ``effect_outcome_unknown`` rather than executing again. Host ports must be
    cooperatively cancellable; they retain responsibility for external cleanup.
    """

    def __init__(
        self,
        *,
        store: RunStore,
        scope: RunScope,
        configuration: RunConfiguration,
        model: ModelPort,
        tools: ToolPort | None = None,
        verifier: VerificationPort | None = None,
        workspace: WorkspacePort | None = None,
        artifacts: ArtifactPort | None = None,
        context_planner: ContextPlanner | None = None,
        context_port: ContextPort | None = None,
        hook_port: HookPort | None = None,
        child_lifecycle_port: ChildLifecyclePort | None = None,
        workspace_revision: str = "unavailable",
        lease_ttl_seconds: float | None = None,
        activation_resource: ActivationResourceRef | None = None,
        model_recovery_capability: ModelTakeoverCapability | None = None,
    ) -> None:
        if configuration.context_sources and (context_port is None or context_planner is None):
            raise ValueError("configured Context sources require a Port and ContextPlanner")
        if any(source.contributor_policy is not None
               and source.contributor_policy.tokenizer_id != context_planner.tokenizer_id
               for source in configuration.context_sources):
            raise ValueError("Contributor tokenizer mismatch")
        if configuration.hooks and not callable(getattr(hook_port, "invoke", None)):
            raise ValueError("configured Hooks require a Hook Port")
        if configuration.child_lifecycle_policy and child_lifecycle_port is None:
            raise ValueError("configured child lifecycle requires a Port")
        if configuration.child_lifecycle_policy == "owned-background/v2" and not callable(
            getattr(child_lifecycle_port, "interrupted_receipts", None)
        ):
            raise ValueError("configured child interruption requires its host Port")
        if configuration.child_promotion_policy and not all(
            callable(getattr(child_lifecycle_port, name, None))
            for name in ("child_tasks", "can_promote", "release_foreground")
        ):
            raise ValueError("configured child promotion requires its host Port")
        if configuration.child_input_policy:
            from ksadk.harness.stores.native_contract import ChildInputRunStore

            if not isinstance(store, ChildInputRunStore) or not all(
                callable(getattr(child_lifecycle_port, name, None))
                for name in ("child_input_target", "wake_child_input")
            ):
                raise ValueError("configured child input requires its atomic Store and host Port")
        if configuration.child_release_policy and not callable(
            getattr(child_lifecycle_port, "release_run", None)
        ):
            raise ValueError("configured child release requires its host Port")
        if configuration.child_notification_policy and not callable(
            getattr(child_lifecycle_port, "ready_children", None)
        ):
            raise ValueError("configured child notifications require their host Port")
        if configuration.child_approval_policy and not getattr(
            child_lifecycle_port, "approval_bridge", False
        ):
            raise ValueError("configured child approval requires its host Port")
        if (configuration.tools and tools is None
                and any(event != "Stop" for hook in configuration.hooks for event in hook.events)):
            raise ValueError("tool Hooks require a Tool Port")
        self.store = store
        self.scope = scope.model_copy(deep=True)
        if activation_resource is not None and activation_resource.scope != self.scope:
            raise ValueError("activation scope mismatch")
        self._activation_resource = activation_resource
        self.configuration = RunConfiguration.model_validate_json(configuration.model_dump_json())
        if configuration.model_recovery_policy is not None and (
            model_recovery_capability is None
            or model_recovery_capability.host_binding_digest != configuration.host_binding_digest
        ):
            raise ValueError("model takeover requires matching host capability")
        self._model_recovery_capability = model_recovery_capability
        self.model, self.tools, self.verifier, self.workspace, self.artifacts = (
            model,
            tools,
            verifier,
            workspace,
            artifacts,
        )
        self.context = ContextDriver(
            context_planner,
            workspace_revision,
            workspace_tracking=workspace is not None,
            replay_context_policy=configuration.profile.provider_replay_projection_policy,
        )
        self._leases = RunLeaseController(store, self.scope, ttl_seconds=lease_ttl_seconds)
        self._effects = NativeEffectDispatcher(
            store, self.scope, self._leases, model, tools, verifier, workspace, artifacts,
            context_port, hook_port,
            child_lifecycle_port,
        )
        self._clock = ActiveRunClock(configuration.limits.max_wall_seconds)
        self._child_promotion = child_lifecycle_port
        self._locks: dict[str, asyncio.Lock] = {}
        self._parallel_progress_locks: dict[str, asyncio.Lock] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._finalization_recovery_fences: dict[str, int] = {}

    def start(
        self,
        *,
        goal: str,
        session_id: str,
        run_id: str | None = None,
        origin: dict | None = None,
        task_contract: TaskContract | None = None,
        expected_parent_run_id: str | None = None,
        pin_parent: bool = False,
        user_input: NativeUserInput | None = None,
    ) -> RunSnapshot:
        config = self.configuration
        if user_input is not None:
            user_input = NativeUserInput.model_validate(user_input)
            if user_input.goal != goal:
                raise ValueError("native_user_input_goal_mismatch")
            if any(document.full_text is not None for document in user_input.documents):
                raise ValueError("native_user_document_capture_required")
            user_input.check_profile(config.profile)
        contract = task_contract or config.contract
        validate_task_tool_scope(contract, config.tools)
        parent = self.store.session_head(self.scope, session_id)
        if pin_parent and (parent.run_id if parent else None) != expected_parent_run_id:
            raise StoreConflict("session changed while compiling task contract")
        if parent is not None and not parent.phase.terminal:
            raise StoreConflict("session already has an active turn")
        history = project_session_history(
            parent, profile_digest=config.profile.digest, configuration_digest=config.digest,
        )
        artifact_refs = list(parent.artifact_refs) if parent else []
        available = {ref["uri"]: ref for ref in artifact_refs}
        for document in user_input.documents if user_input is not None else ():
            if document.text_artifact is not None:
                ref = document.text_artifact.model_dump(mode="json")
                if ref["uri"] in available and available[ref["uri"]] != ref:
                    raise ValueError("native_user_document_artifact_conflict")
                if ref["uri"] not in available:
                    artifact_refs.append(ref)
                    available[ref["uri"]] = ref
        snapshot = RunSnapshot(
            scope=self.scope,
            run_id=run_id or str(uuid4()),
            session_id=session_id,
            turn_id=str(uuid4()),
            parent_run_id=parent.run_id if parent else None,
            goal=goal,
            initial_goal_digest=hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            messages=(*history, *(user_input.messages() if user_input is not None
                                 else ({"role": "user", "content": goal},))),
            task_progress=parent.task_progress if parent else (),
            task_progress_revision=parent.task_progress_revision if parent else 0,
            workspace_revision=(
                parent.workspace_revision
                if parent
                else (
                    self.context.workspace_revision
                    if self.context.workspace_revision != "unavailable"
                    else None
                )
            ),
            artifact_refs=tuple(artifact_refs),
            workspace_changed_paths=parent.workspace_changed_paths if parent else (),
            workspace_observation_status=(
                parent.workspace_observation_status
                if parent
                else (
                    "initial" if self.context.workspace_revision != "unavailable" else "unavailable"
                )
            ),
            workspace_observation_reason=(parent.workspace_observation_reason if parent else None),
            task_contract=contract.model_dump(mode="json"),
            context_binding=self.context.binding,
            profile_digest=config.profile.digest,
            contract_digest=digest(contract),
            configuration_digest=config.digest,
            origin=origin or {},
            activation_resource_ref=(
                self._activation_resource.model_dump(mode="json")
                if self._activation_resource is not None else None
            ),
        )
        self.store.create_turn(snapshot, expected_parent_run_id=parent.run_id if parent else None)
        self.attach(snapshot.run_id)
        return self.snapshot(snapshot.run_id)

    def fork(
        self,
        *,
        source_run_id: str,
        expected_source_revision: int,
        session_id: str,
        goal: str,
        command_id: str | None = None,
        run_id: str | None = None,
        origin: dict | None = None,
        task_contract: TaskContract | None = None,
    ) -> ForkCreation:
        """Start a new branch at an immutable terminal Run, not its latest head.

        New configuration comes from this destination host. Historical execution
        state and resources remain with the source; this does not rewind files,
        resume an interrupted side effect, or import another runtime's snapshot.
        A stable command_id returns the original branch on retries; only a
        created receipt attaches execution ownership. Without an ID each call
        is a new command, subject to the unique destination Session boundary.
        """
        if not isinstance(self.store, ForkingRunStore):
            raise StoreConflict("store does not support atomic fork creation")
        if type(expected_source_revision) is not int or expected_source_revision < 0:
            raise StoreConflict("fork source revision must be a nonnegative integer")
        source = self.store.load(self.scope, source_run_id)
        check_scope(self.scope, source)
        if not source.phase.terminal:
            raise StoreConflict("fork requires a terminal source run")
        if source.revision != expected_source_revision:
            raise StoreConflict("fork source revision changed")
        contract = task_contract or self.configuration.contract
        validate_task_tool_scope(contract, self.configuration.tools)
        if command_id is not None:
            if (not isinstance(command_id, str) or not command_id.strip()
                    or len(command_id) > 160):
                raise StoreConflict("fork command ID must be nonempty and at most 160 characters")
            addressed_run = command_run_id(self.scope, source_run_id, command_id)
            if run_id is not None and run_id != addressed_run:
                raise StoreConflict("fork command must use its scoped Run identity")
            run_id = addressed_run
        snapshot = build_fork_snapshot(
            source, run_id=run_id or str(uuid4()), turn_id=str(uuid4()), session_id=session_id,
            goal=goal, configuration=self.configuration, contract=contract,
            context_binding=self.context.binding,
            workspace_revision=self.context.workspace_revision,
            activation_resource=self._activation_resource, origin=origin, command_id=command_id,
        )
        result = self.store.create_fork(snapshot)
        if result.created:
            self.attach(result.snapshot.run_id)
        return result

    def attach(self, run_id: str) -> RunSnapshot:
        snapshot = self.snapshot(run_id)
        if not self._leases.attached(run_id):
            self._leases.acquire(run_id)
            self._locks[run_id] = asyncio.Lock()
            self._wakes[run_id] = asyncio.Event()
            self._clock.attach(snapshot)
            if self.workspace is not None and RunPhase.FINALIZING in {
                snapshot.phase, snapshot.paused_from, snapshot.resume_phase,
            }:
                self._finalization_recovery_fences[run_id] = self.fencing_token(run_id)
        return snapshot

    def release(self, run_id: str) -> None:
        self._clock.release(run_id)
        self._leases.release(run_id)
        self._locks.pop(run_id, None)
        self._wakes.pop(run_id, None)
        self._finalization_recovery_fences.pop(run_id, None)

    async def retain_ownership(self, run_id: str, released: asyncio.Event) -> None:
        """An attached host remains owner during user-controlled suspension.

        Model/tool effects have their own bounded cancellation scopes. This
        outer scope also covers gaps between effects, approvals and pauses;
        it grants no execution and never reacquires a lost fencing epoch.
        """
        self.fencing_token(run_id)
        await self._leases.keep_alive(run_id, released.wait())

    @property
    def leased_ownership(self) -> bool:
        return self._leases.ttl_seconds is not None

    async def recover_activation_resources(
        self, run_id: str, current: ActivationResourceRef | None,
        available: Callable[[], Awaitable[bool]] | None,
    ) -> RunSnapshot:
        from .activation_recovery import recovery_transition

        self.fencing_token(run_id)
        async with self._locks[run_id]:
            snapshot = self.snapshot(run_id)
            batch = recovery_transition(snapshot, current)
            if current is not None:
                if available is None:
                    raise ValueError("activation availability probe required")
                try:
                    live = await self._leases.keep_alive(run_id, available())
                except Exception:
                    raise ValueError("activation resource unavailable") from None
                if live is not True:
                    raise ValueError("activation resource unavailable")
            return snapshot if batch is None else self._commit(snapshot, batch)

    async def recover_browser_resources(
        self, run_id: str, port: BrowserRecoveryPort | None
    ) -> RunSnapshot:
        from .browser_contract import BrowserRecovery, BrowserSessionRef
        from .browser_recovery import recovery_transition

        self.fencing_token(run_id)
        async with self._locks[run_id]:
            snapshot = self.snapshot(run_id)
            refs = tuple(BrowserSessionRef.model_validate(item)
                         for item in snapshot.browser_session_refs)
            if any(ref.scope != snapshot.scope or ref.run_id != run_id for ref in refs):
                raise ValueError("persisted browser scope mismatch")
            reports = (
                await self._leases.keep_alive(run_id, port.recover_sessions(refs))
                if port is not None and refs else tuple(
                    BrowserRecovery(status="lost", session_ref=ref, reason="browser_port_unbound")
                    for ref in refs
                )
            )
            # Claimed effects must still acquire an unknown-outcome receipt;
            # a resource failure cannot conceal that uncertainty.
            pending = (self.store.get_intent(self.scope, run_id, snapshot.pending_intent)
                       if snapshot.pending_intent else None)
            batch = recovery_transition(snapshot, reports)
            if batch is None:
                return snapshot
            if pending is not None and pending.status == "running":
                batch = batch.model_copy(update={"snapshot": snapshot.model_copy(update={
                    "revision": batch.snapshot.revision, "event_seq": batch.snapshot.event_seq,
                    "last_browser_observation": None,
                })})
            return self._commit(snapshot, batch)

    def snapshot(self, run_id: str) -> RunSnapshot:
        snapshot = self.store.load(self.scope, run_id)
        if snapshot.configuration_digest != self.configuration.digest:
            raise ValueError(
                "run configuration is immutable; attach requires the original configuration"
            )
        if snapshot.context_binding != self.context.binding:
            raise ValueError("context tokenizer, policy or workspace binding changed")
        return snapshot

    def effective_configuration(self, snapshot: RunSnapshot) -> RunConfiguration:
        return effective_run_configuration(self.configuration, snapshot)

    def steer(
        self,
        run_id: str,
        *,
        text: str,
        command_id: str,
        expected_goal_revision: int,
        task_contract: TaskContract | None = None,
    ):
        from .steering import SteeringUpdate

        update = SteeringUpdate(
            text=text, expected_goal_revision=expected_goal_revision, task_contract=task_contract
        )
        return self.submit(
            run_id,
            RunCommand(command_id=command_id, kind="steer", payload=update.model_dump(mode="json")),
        )

    def reserved_goal_revision(self, run_id: str) -> int:
        """Project queued user corrections without weakening direct CAS commands."""
        from .steering import reserved_goal_revision

        return reserved_goal_revision(
            self.snapshot(run_id), self.store.commands(self.scope, run_id)
        )

    def guide(self, run_id: str, *, text: str, command_id: str, expected_goal_revision: int):
        from .guidance import GuidanceInput

        payload = GuidanceInput(text=text, expected_goal_revision=expected_goal_revision)
        if self.configuration.guidance_policy is None:
            return CommandReceipt(command_id=command_id, status="rejected",
                                  reason="guidance_policy_unavailable")
        return self.submit(run_id, RunCommand(command_id=command_id, kind="guide",
                                             payload=payload.model_dump(mode="json")))

    def _runnable_commands(self, snapshot: RunSnapshot):
        from .guidance import runnable_commands

        return runnable_commands(snapshot, self.store.commands(self.scope, snapshot.run_id),
                                 guidance_enabled=self.configuration.guidance_policy is not None)

    def child_tasks(self, run_id: str) -> tuple[dict, ...]:
        snapshot = self.snapshot(run_id)
        reader = getattr(self._child_promotion, "child_tasks", None)
        if not callable(reader):
            raise ValueError("child_directory_unavailable")
        rows = reader(snapshot)
        if self.configuration.child_promotion_policy is None:
            rows = tuple({**row, "can_promote": False} for row in rows)
        if self.configuration.child_input_policy is None:
            rows = tuple({**row, "can_send_input": False} for row in rows)
        if not self.supports_child_cancellation():
            rows = tuple({**row, "can_cancel": False} for row in rows)
        return rows

    def supports_child_cancellation(self) -> bool:
        return self.configuration.child_lifecycle_policy == "owned-background/v2" and all(
            callable(getattr(self._child_promotion, name, None))
            for name in ("can_cancel", "cancel_child")
        )

    def cancel_child(self, run_id: str, *, child_run_id: str, command_id: str):
        from .child_promotion import ChildPromotionInput

        payload = ChildPromotionInput(child_run_id=child_run_id)
        snapshot = self.snapshot(run_id)
        if not self.supports_child_cancellation() or not any(
            row["child_run_id"] == child_run_id
            for row in self._child_promotion.child_tasks(snapshot)
        ):
            return CommandReceipt(command_id=command_id, status="rejected",
                                  reason="child_not_owned_or_cancellation_unavailable")
        return self.submit(run_id, RunCommand(command_id=command_id, kind="cancel_child",
                                             payload=payload.model_dump(mode="json")))

    async def _consume_child_cancellation(self, run_id: str, command: RunCommand):
        from .child_cancellation import consume_child_cancellation

        return await consume_child_cancellation(
            self.snapshot(run_id), command, enabled=self.supports_child_cancellation(),
            port=self._child_promotion, commit=self._commit,
        )

    def child_execution_view(self, run_id: str, child_run_id: str) -> dict:
        reader = getattr(self._child_promotion, "child_execution_view", None)
        if not callable(reader):
            raise ValueError("child_execution_view_unavailable")
        return reader(self.snapshot(run_id), child_run_id)

    async def release_children(self, run_id: str) -> None:
        from .child_lifecycle_contract import ChildResourceReleaseRequest

        # Reading the pinned Run does not reacquire a lost parent lease.
        # Cleanup stops locally owned child tasks; it cannot terminalize or
        # reconcile facts under a replacement parent's fence.
        self.snapshot(run_id)
        if self.configuration.child_release_policy is not None:
            await self._child_promotion.release_run(
                ChildResourceReleaseRequest(scope=self.scope, run_id=run_id)
            )

    def promote_child(self, run_id: str, *, child_run_id: str, command_id: str):
        from .child_promotion import ChildPromotionInput

        payload = ChildPromotionInput(child_run_id=child_run_id)
        if self.configuration.child_promotion_policy is None:
            return CommandReceipt(command_id=command_id, status="rejected",
                                  reason="child_promotion_policy_unavailable")
        return self.submit(run_id, RunCommand(command_id=command_id, kind="promote_child",
                                             payload=payload.model_dump(mode="json")))

    def _consume_child_promotion(self, run_id: str, command: RunCommand) -> RunSnapshot:
        from .child_promotion import consume_promotion

        return consume_promotion(
            self.snapshot(run_id), command, port=self._child_promotion,
            enabled=self.configuration.child_promotion_policy is not None, commit=self._commit,
        )

    def send_child_input(self, run_id, *, child_run_id, text, command_id, expected_goal_revision):
        from .child_input import ChildInput

        payload = ChildInput(child_run_id=child_run_id, text=text,
                             expected_goal_revision=expected_goal_revision)
        if self.configuration.child_input_policy is None:
            return CommandReceipt(command_id=command_id, status="rejected",
                                  reason="child_input_policy_unavailable")
        return self.submit(run_id, RunCommand(command_id=command_id, kind="message_child",
                                             payload=payload.model_dump(mode="json")))

    def _consume_child_input(self, run_id, command):
        from .child_input_runtime import consume_child_input

        return consume_child_input(self.snapshot(run_id), command, port=self._child_promotion,
                                   enabled=self.configuration.child_input_policy is not None,
                                   commit=self._commit)

    def fencing_token(self, run_id: str) -> int:
        return self._leases.token(run_id)

    def events(self, run_id: str, after_seq: int = 0) -> tuple[NativeEvent, ...]:
        self.snapshot(run_id)
        return self.store.events(self.scope, run_id, after_seq=after_seq)

    def submit(self, run_id: str, command: RunCommand) -> CommandReceipt:
        self.snapshot(run_id)
        receipt = self.store.enqueue(self.scope, run_id, command)
        if wake := self._wakes.get(run_id):
            wake.set()
        return receipt

    def notify_pending_commands(self, run_id: str) -> None:
        """Wake a locally owned driver after an atomic external inbox commit."""
        self.snapshot(run_id)
        if wake := self._wakes.get(run_id):
            wake.set()

    async def run_until_idle(self, run_id: str, *, max_steps: int = 1000) -> RunSnapshot:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        for _ in range(max_steps):
            before = self.snapshot(run_id)
            after = await self.advance(run_id)
            if after.phase.terminal or after.revision == before.revision:
                return after
        raise RuntimeError("driver step budget reached; run remains resumable")

    async def _wait_retry(self, snapshot: RunSnapshot) -> RunSnapshot | None:
        """Wait under the driver's lease, while still admitting control commands."""
        while snapshot.retry_not_before > time():
            remaining = self._clock.remaining(snapshot)
            if remaining is not None and remaining <= 0:
                return self._commit(snapshot, wall_budget_stop(
                    snapshot, self.store, self.scope, elapsed=self._clock.elapsed(snapshot)
                ))
            wake = self._wakes[snapshot.run_id]
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    wake.wait(), min(max(0, snapshot.retry_not_before - time()), 0.1)
                )
            wake.clear()
            commands = self._runnable_commands(snapshot)
            if commands:
                return await self._apply_command(snapshot, commands[0])
        return None

    async def advance(self, run_id: str) -> RunSnapshot:
        self.fencing_token(run_id)
        async with self._locks[run_id]:
            snapshot = self.snapshot(run_id)
            if snapshot.phase.terminal:
                return snapshot
            pending = (self.store.get_intent(self.scope, run_id, snapshot.pending_intent)
                       if snapshot.pending_intent else None)
            if (pending is not None and pending.kind == "child"
                    and pending.payload["request"]["action"] == "cancel"):
                # Complete the parent-owned child effect before consuming the
                # cancellation it was staged to settle.
                return await self._execute(snapshot)
            commands = self._runnable_commands(snapshot)
            if commands:
                return await self._apply_command(snapshot, commands[0])
            if snapshot.phase in {RunPhase.PAUSED, RunPhase.AWAITING_INTERACTION}:
                return snapshot
            if snapshot.phase != RunPhase.FINALIZING and (
                (remaining := self._clock.remaining(snapshot)) is not None and remaining <= 0
            ):
                return self._commit(snapshot, wall_budget_stop(
                    snapshot, self.store, self.scope, elapsed=self._clock.elapsed(snapshot)
                ))
            if snapshot.pending_intent:
                probe = child_ready_probe(snapshot, self.effective_configuration(snapshot), pending)
                if probe is not None:
                    ready = self._child_promotion.ready_children(probe)
                    batch = stage_child_ready(snapshot, pending, probe, ready)
                    if batch is not None:
                        return self._commit(snapshot, batch)
                return await self._execute(snapshot)
            if snapshot.phase != RunPhase.FINALIZING and snapshot.retry_not_before > time():
                stopped = await self._leases.keep_alive(run_id, self._wait_retry(snapshot))
                if stopped is not None:
                    return stopped
            config = self.effective_configuration(snapshot)
            child_batch = prepare_child_lifecycle(snapshot, config)
            if child_batch is not None:
                return self._commit(snapshot, child_batch)
            if snapshot.phase != RunPhase.FINALIZING and (
                (remaining := self._clock.remaining(snapshot)) is not None and remaining <= 0
            ):
                return self._commit(snapshot, wall_budget_stop(
                    snapshot, self.store, self.scope, elapsed=self._clock.elapsed(snapshot)
                ))
            if (snapshot.phase == RunPhase.FINALIZING
                    and run_id in self._finalization_recovery_fences):
                batch = prepare_finalization_workspace(
                    snapshot, self._finalization_recovery_fences[run_id]
                ) or next_transition(snapshot, config)
            elif snapshot.phase == RunPhase.PREPARING_CONTEXT:
                batch = self.context.prepare(snapshot, config)
            elif snapshot.phase == RunPhase.COMPACTING:
                batch = (prepare_precompact_hook(snapshot, config)
                         or self.context.begin_summary(snapshot, config))
            elif snapshot.phase == RunPhase.VERIFYING and self.verifier is not None:
                batch = (prepare_stop_hook(snapshot, config)
                         or self.context.prepare_verification(snapshot)
                         or next_transition(snapshot, config))
            else:
                batch = next_transition(snapshot, config)
            if batch is None:
                return snapshot
            if (self._model_recovery_capability is not None and batch.intents
                    and batch.intents[0].kind == "model"):
                now = time()
                quote = self._clock.quote(snapshot, wall_time=now, clock_domain_digest=
                                          self._model_recovery_capability.clock_domain_digest)
                planned = prepare_reserved_model(
                    snapshot, self.configuration, self._model_recovery_capability, now=now,
                    payload=batch.intents[0].payload, clock_quote=quote,
                    commands=self.store.commands(self.scope, run_id),
                )
                # Ineligible requests keep their original behavior and are not
                # retroactively granted takeover on a later process restart.
                if planned.batch is not None:
                    batch = planned.batch
            try:
                return self._commit(snapshot, batch)
            except PendingCommandConflict:
                # enqueue() and commit() are serialized by the Store. If a
                # command was accepted after the driver's first poll, consume
                # it before publishing the final answer or terminal event.
                commands = self._runnable_commands(snapshot)
                if not commands:
                    raise
                return await self._apply_command(snapshot, commands[0])

    def _commit(self, previous: RunSnapshot, batch: CommitBatch, *, child_input=None):
        batch, now = self._clock.prepare_commit(previous, batch)
        commit = self.store.commit if child_input is None else self.store.commit_child_input
        committed = commit(
            self.scope,
            batch,
            expected_revision=previous.revision,
            fencing_token=self.fencing_token(previous.run_id),
            **({"delivery": child_input} if child_input is not None else {}),
        )
        self._clock.committed(committed, now)
        return committed

    async def _apply_command(self, snapshot: RunSnapshot, command: RunCommand) -> RunSnapshot:
        if command.kind == "cancel_child":
            return await self._consume_child_cancellation(snapshot.run_id, command)
        if command.kind == "message_child":
            return self._consume_child_input(snapshot.run_id, command)
        if command.kind == "promote_child":
            return self._consume_child_promotion(snapshot.run_id, command)
        if (self.configuration.child_lifecycle_policy == "owned-background/v2"
                and command.kind in {"cancel", "policy_revoked"} and snapshot.pending_intent):
            intent = self.store.get_intent(self.scope, snapshot.run_id, snapshot.pending_intent)
            if (intent is not None and intent.kind == "tool" and intent.status == "running"
                    and self.store.get_receipt(self.scope, snapshot.run_id,
                                              intent.operation_key) is None):
                interrupted = EffectReceipt(operation_key=intent.operation_key, status="uncertain",
                                            result={"reason": "control_interrupted_effect"})
                received = self._interrupted_child_receipt(snapshot, intent, interrupted)
                if received is not interrupted or self.snapshot(snapshot.run_id) != snapshot:
                    # Keep the original accepted command queued until the
                    # known receipts and any owned allocations are settled.
                    return self._commit_receipt(
                        self.snapshot(snapshot.run_id), intent, received,
                        interruption=interrupted if received is not interrupted else None,
                    )
        if (self.configuration.child_lifecycle_policy is not None
                and command.kind in {"cancel", "policy_revoked"}
                and snapshot.child_lifecycle_failure != "cancel"
                and any(item.settled_usage_digest is None
                        for item in snapshot.child_budget_reservations)):
            if snapshot.pending_intent:
                return self._commit(snapshot, stage_child_cancellation(
                    self.store, self.scope, snapshot, command,
                    bound_tool_names=frozenset(tool.name for tool in self.configuration.tools),
                ))
            batch = prepare_child_lifecycle(snapshot, self.effective_configuration(snapshot),
                                            cancel=True)
            if batch is not None:
                return self._commit(snapshot, batch)
        committed = self._commit(
            snapshot,
            resolve_command_batch(
                self.store,
                self.scope,
                snapshot,
                command,
                bound_tool_names=frozenset(tool.name for tool in self.configuration.tools),
                guidance_enabled=self.configuration.guidance_policy is not None,
                replay_configuration=self.configuration,
            ),
        )
        if (self.workspace is not None and command.kind == "pause"
                and committed.paused_from == RunPhase.FINALIZING):
            self._finalization_recovery_fences[snapshot.run_id] = self.fencing_token(
                snapshot.run_id
            )
        return committed

    async def _execute(self, snapshot: RunSnapshot) -> RunSnapshot:
        key = snapshot.pending_intent
        intent = self.store.get_intent(self.scope, snapshot.run_id, key)
        if intent is None:
            raise RuntimeError("committed snapshot references a missing intent")
        receipt = self.store.get_receipt(self.scope, snapshot.run_id, key)
        reconcile_tool = (
            receipt is None
            and intent.status == "running"
            and intent.kind == "tool"
            and isinstance(intent.payload.get("call"), dict)
            and self.configuration.tool_recovery_policy in {
                "sealed_local_child/v1", "prepared_local_child/v1",
                "resume_prepared_local_child/v1", "resume_local_child_checkpoint/v1",
            }
        )
        if receipt is None and intent.status == "running" and intent.kind != "workspace":
            if intent.kind == "model" and self._model_recovery_capability is not None:
                now = time()
                planned = settle_lost_model(
                    snapshot, intent, receipt, self.configuration,
                    self._model_recovery_capability, now=now,
                    clock_quote=self._clock.quote(snapshot, wall_time=now, clock_domain_digest=
                                                  self._model_recovery_capability.clock_domain_digest),
                    commands=self.store.commands(self.scope, snapshot.run_id),
                )
                if planned.batch is not None:
                    try:
                        return self._commit(snapshot, planned.batch)
                    except PendingCommandConflict:
                        commands = self.store.commands(self.scope, snapshot.run_id)
                        if not commands:
                            raise
                        return await self._apply_command(snapshot, commands[0])
            if not reconcile_tool:
                receipt = EffectReceipt(
                    operation_key=key, status="uncertain",
                    result={"reason": "effect_outcome_unknown"},
                )
        if receipt is None:
            if intent.status == "prepared":
                try:
                    self.store.claim_intent(
                        self.scope, snapshot.run_id, key,
                        fencing_token=self.fencing_token(snapshot.run_id),
                        **({"require_no_pending_commands": True}
                           if (TAKEOVER_RESERVATION in intent.payload
                               or (intent.kind == "workspace"
                                   and intent.payload.get("purpose") == "finalization")) else {}),
                    )
                except PendingCommandConflict:
                    commands = self.store.commands(self.scope, snapshot.run_id)
                    if not commands:
                        raise
                    return await self._apply_command(snapshot, commands[0])
                if intent.payload.get("parallel_readonly") is True:
                    for index, _call in enumerate(intent.payload["calls"], 1):
                        member_key = f"{key}:call:{index}"
                        # Old prepared read batches have no member intents.
                        # Their unknown fallback does not invent new admission.
                        if self.store.get_intent(
                            self.scope, snapshot.run_id, member_key,
                        ) is not None:
                            self.store.claim_intent(
                                self.scope, snapshot.run_id, member_key,
                                fencing_token=self.fencing_token(snapshot.run_id),
                            )
            receipt = await self._effects.dispatch(
                snapshot,
                intent,
                self.effective_configuration(snapshot),
                wake=self._wakes[snapshot.run_id],
                emit=lambda event: self._stream_delta(snapshot.run_id, event),
                on_child_promotion=lambda command: self._consume_child_promotion(
                    snapshot.run_id, command
                ),
                on_child_input=lambda command: self._consume_child_input(snapshot.run_id, command),
                on_child_cancellation=lambda command: self._consume_child_cancellation(
                    snapshot.run_id, command,
                ),
                remaining_wall_seconds=self._clock.remaining(snapshot),
                reconcile_tool=reconcile_tool,
                on_parallel_child=(
                    lambda index, raw_call, operation_key, result: self._commit_parallel_child(
                        snapshot.run_id, intent, index, raw_call, operation_key, result
                    )
                    if intent.payload.get("parallel_readonly") is True
                    else None
                ),
            )
        interrupted = receipt
        receipt = self._interrupted_child_receipt(self.snapshot(snapshot.run_id), intent, receipt)
        return self._commit_receipt(
            self.snapshot(snapshot.run_id), intent, receipt, reconciled=reconcile_tool,
            interruption=interrupted if receipt is not interrupted else None,
        )
