"""Opt-in ExecutionEngine facade projecting the Native ledger.

Plain text and explicitly typed user media are supported. Explicit legacy policies are
rejected; omitted defaults are not applied, with a compile warning. The injected
RunConfiguration and authenticated host ports own budgets, tools and policy.
Host ports must enforce authorization/sandboxing, including approval decisions.

Streaming delivers committed provisional deltas on opted-in model profiles. Native has
no wall-clock event timestamps, so projections use timestamp=0 with provenance.
Approval events are projected from native commits; unimplemented tool begin/end
events are not fabricated from later snapshots.
Handles and event cursors can be restored with matching scope/config/spec pins.
An interrupted external effect without a receipt remains uncertain after attach.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Awaitable, Callable
from typing import AsyncIterator
from uuid import uuid4

from ksadk.harness.engine.base import (
    CompiledHarness,
    EngineCapability,
    EngineCapabilityMatrix,
    ExecutionEngineError,
    ExecutionPlan,
)
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.native.activation_contract import ActivationResourceRef
from ksadk.harness.native.background_command_contract import BackgroundCommandPort
from ksadk.harness.native.child_lifecycle import ChildLifecyclePort
from ksadk.harness.native.completion import TaskContract
from ksadk.harness.native.context_contributions import ContextPort
from ksadk.harness.native.context_runtime import ContextPlanner
from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.hook_runtime_contract import HookPort
from ksadk.harness.native.model_takeover import ModelTakeoverCapability
from ksadk.harness.native.ports import (
    ArtifactPort,
    BrowserRecoveryPort,
    ModelPort,
    RunConfiguration,
    ToolPort,
    VerificationPort,
    WorkspacePort,
)
from ksadk.harness.native.restore_contract import SNAPSHOT_READER_VERSION
from ksadk.harness.native.state import NativeEvent, RunCommand, RunPhase, RunScope, RunSnapshot
from ksadk.harness.native.user_document_capture import capture_user_documents
from ksadk.harness.native.user_input import NativeUserInput, native_input_goal
from ksadk.harness.native_driver_failure import NativeDriverFailure, driver_failure
from ksadk.harness.spec import HARNESS_VERSION, SCHEMA_VERSION, HarnessSpec
from ksadk.harness.state import (
    ApprovalRequest,
    CapabilitySnapshot,
    HarnessState,
    Message,
    RunStatus,
    ToolCall,
    WorkingContext,
)
from ksadk.harness.stores.native_contract import (
    NotificationTurnCreation,
    RestoreAuditStore,
    RunStore,
    StoreConflict,
)
from ksadk.runtime import (
    CancelResult,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    StartRequest,
)

_LEGACY_POLICIES = {
    "context_policy",
    "memory_policy",
    "capabilities",
    "approval_policy",
    "sandbox_policy",
    "observability_policy",
}
_TERMINALS = {
    "run.completed": EventType.RUN_COMPLETED,
    "run.failed": EventType.RUN_FAILED,
    "run.canceled": EventType.RUN_CANCELED,
    "run.interrupted": EventType.RUN_INTERRUPTED,
}

_SAME_TURN_MODEL_RESPONSES = frozenset(
    {
        "provider_retryable_error",
        "empty_response",
        "malformed_tool_call",
        "output_limit",
        "context_overflow",
    }
)


def _logical_turn_count(events: tuple[NativeEvent, ...]) -> int:
    """Project logical model turns without counting transport retries or summaries.

    A model attempt belongs to the previous turn while it is recovering from
    an incomplete/failed response or continuing truncated output. An accepted
    tool batch or final answer closes that turn. Legacy ledgers lack
    ``model_purpose``; their adjacent compaction event identifies summaries.
    """
    turns = 0
    continuing = False
    for index, event in enumerate(events):
        if event.kind != "effect.received" or event.payload.get("kind") != "model":
            continue
        if event.payload.get("model_purpose") == "compaction" or (
            "model_purpose" not in event.payload
            and index + 1 < len(events)
            and events[index + 1].kind.startswith("context.compaction_")
        ):
            continue
        response_kind = event.payload.get("model_response_kind")
        if not isinstance(response_kind, str):
            # A joined timeout has no sealed result, but still consumed one
            # attempt in the same logical turn as its eventual recovery.
            response_kind = "provider_retryable_error"
        if not continuing:
            turns += 1
        continuing = response_kind in _SAME_TURN_MODEL_RESPONSES
    return min(turns, 1024)


class NativeHarnessEngine:
    """ExecutionEngine facade for one host-authorized scope/configuration.

    Spec model references must be ``model-profile://<profile_id>@<version>``;
    inline instructions must exactly match RunConfiguration.instructions.
    Start metadata/config, history, attachments and model overrides are not
    accepted. Use a separate explicitly configured engine for another model.
    ``close`` closes a run, not the externally owned shared Store or ports.
    """

    def __init__(
        self,
        *,
        scope: RunScope,
        configuration: RunConfiguration,
        model: ModelPort,
        store: RunStore,
        tools: ToolPort | None = None,
        verifier: VerificationPort | None = None,
        workspace: WorkspacePort | None = None,
        artifacts: ArtifactPort | None = None,
        context_planner: ContextPlanner | None = None,
        context_port: ContextPort | None = None,
        hook_port: HookPort | None = None,
        child_lifecycle_port: ChildLifecyclePort | None = None,
        background_command_port: BackgroundCommandPort | None = None,
        workspace_revision: str = "unavailable",
        lease_ttl_seconds: float | None = None,
        browser_recovery: BrowserRecoveryPort | None = None,
        activation_resource: ActivationResourceRef | None = None,
        activation_available: Callable[[], Awaitable[bool]] | None = None,
        model_recovery_capability: ModelTakeoverCapability | None = None,
    ) -> None:
        if configuration.tools and tools is None:
            raise ExecutionEngineError("configured native tools require a host ToolPort")
        if activation_resource is not None and activation_available is None:
            raise ExecutionEngineError("activation availability probe required")
        self._kernel = NativeKernel(
            scope=scope,
            configuration=configuration,
            model=model,
            tools=tools,
            verifier=verifier,
            workspace=workspace,
            artifacts=artifacts,
            store=store,
            context_planner=context_planner,
            context_port=context_port,
            hook_port=hook_port,
            child_lifecycle_port=child_lifecycle_port,
            background_command_port=background_command_port,
            workspace_revision=workspace_revision,
            lease_ttl_seconds=lease_ttl_seconds,
            activation_resource=activation_resource,
            model_recovery_capability=model_recovery_capability,
        )
        self._compiled: dict[int, tuple[CompiledHarness, str]] = {}
        self._browser_recovery = browser_recovery
        self._activation_resource = activation_resource
        self._activation_available = activation_available
        self._handles: dict[str, RunHandle] = {}
        self._drivers: dict[str, asyncio.Task[None]] = {}
        self._ownership: dict[str, tuple[asyncio.Event, asyncio.Task[None]]] = {}
        self._public_output_sinks: dict[str, object] = {}
        self._releases: dict[str, asyncio.Task[None]] = {}
        self._changed: dict[str, asyncio.Event] = {}
        self._cursors: dict[str, int] = {}
        self._streaming: set[str] = set()
        self._closed: set[str] = set()
        self._fork_observers: set[str] = set()
        self._warned = False

    @property
    def _profile_ref(self) -> str:
        profile = self._kernel.configuration.profile
        return f"model-profile://{profile.profile_id}@{profile.profile_version}"

    async def compile(self, spec: HarnessSpec) -> CompiledHarness:
        self._validate_spec(spec)
        if not self._warned:
            warnings.warn(
                "Native experimental engine uses the injected RunConfiguration and host policy "
                "ports; legacy defaults are not applied",
                UserWarning,
                stacklevel=2,
            )
            self._warned = True
        plan = ExecutionPlan(
            strategy_kind="single-agent",
            nodes=("prepare_context", "request_model", "execute_tools", "verify", "final"),
            edges=(
                ("prepare_context", "request_model"),
                ("request_model", "execute_tools"),
                ("execute_tools", "prepare_context"),
                ("request_model", "verify"),
                ("verify", "prepare_context"),
                ("verify", "final"),
            ),
        )
        result = CompiledHarness(spec=spec.model_copy(deep=True), plan=plan, engine_kind="native")
        self._compiled[id(result)] = (result, result.spec.content_hash())
        return result

    def _validate_spec(self, spec: HarnessSpec) -> None:
        explicit = _LEGACY_POLICIES.intersection(spec.model_fields_set)
        if explicit:
            raise ExecutionEngineError(
                "unsupported explicit native policies: " + ", ".join(sorted(explicit))
            )
        if spec.schema_version != SCHEMA_VERSION or spec.harness_version != HARNESS_VERSION:
            raise ExecutionEngineError("unsupported HarnessSpec version")
        if spec.execution_strategy.kind != "single-agent" or spec.execution_strategy.config:
            raise ExecutionEngineError(
                "native facade supports only the single-agent strategy without config"
            )
        if spec.sub_agents:
            raise ExecutionEngineError("native sub_agents are unsupported")
        model = spec.model
        if model.profile_ref != self._profile_ref:
            raise ExecutionEngineError(
                "spec model profile must match the injected profile ID and version"
            )
        if model.temperature is not None or model.fallback_profile_refs:
            raise ExecutionEngineError("native temperature and fallback profiles are unsupported")
        if "provider_policy" in model.model_fields_set:
            raise ExecutionEngineError("explicit legacy provider_policy is unsupported")
        if model.max_output_tokens not in {
            None,
            self._kernel.configuration.profile.max_output_tokens,
        }:
            raise ExecutionEngineError(
                "spec max_output_tokens conflicts with the pinned ModelProfile"
            )
        if (
            spec.prompt.instructions_ref
            or spec.prompt.instructions != self._kernel.configuration.instructions
        ):
            raise ExecutionEngineError(
                "native spec requires inline instructions matching RunConfiguration"
            )

    async def start(
        self,
        request: StartRequest,
        compiled: CompiledHarness,
        *,
        task_contract: TaskContract | None = None,
        expected_parent_run_id: str | None = None,
        pin_parent: bool = False,
        task_chain_id: str | None = None,
    ) -> RunHandle:
        pinned = self.validate_start_request(request, compiled)
        # The host may bind a single trusted ``source_run_id`` in the request
        # metadata for an explicit continuation. validate_start_request admits
        # only that key; resolve it here so a direct SDK route cannot silently
        # drop a requested continuation. Load and validate the source run, then
        # derive the inherited chain/parent identity. Explicitly-passed
        # continuation kwargs (the adapter validates the source itself before
        # calling start) must agree with the derived values, so a validated
        # binding is never contradicted and an unbound direct route never drops.
        metadata_source_run_id = (
            request.metadata.get("source_run_id") if request.metadata else None
        )
        if metadata_source_run_id is not None:
            source = self._kernel.store.load(
                self._kernel.scope, metadata_source_run_id
            )
            if source.scope != self._kernel.scope:
                raise ExecutionEngineError(
                    "continuation source run scope mismatch"
                )
            if source.session_id != request.session_id:
                raise ExecutionEngineError(
                    "continuation source run session mismatch"
                )
            if not source.phase.terminal:
                raise ExecutionEngineError(
                    "continuation source run is not terminal"
                )
            if source.phase in {RunPhase.CANCELED, RunPhase.INTERRUPTED, RunPhase.FAILED}:
                raise ExecutionEngineError(
                    "continuation source run is canceled or failed"
                )
            if not source.task_chain_id:
                raise ExecutionEngineError(
                    "continuation source run has no task_chain_id"
                )
            if task_chain_id is not None and task_chain_id != source.task_chain_id:
                raise ExecutionEngineError(
                    "continuation source_run_id conflicts with task_chain_id"
                )
            if (expected_parent_run_id is not None
                    and expected_parent_run_id != source.run_id):
                raise ExecutionEngineError(
                    "continuation source_run_id conflicts with expected_parent_run_id"
                )
            task_chain_id = source.task_chain_id
            expected_parent_run_id = source.run_id
            pin_parent = True
        elif task_chain_id is None:
            # No trusted continuation binding: an ordinary start gets a fresh
            # unique chain, so the direct SDK route owns chain identity exactly
            # as the adapter does. A caller that supplies an explicit chain
            # (the adapter) keeps it unchanged.
            task_chain_id = f"chain_{uuid4().hex}"
        await self._check_activation_available()
        run_id = None
        user_input = request.input if isinstance(request.input, NativeUserInput) else None
        if user_input is not None and any(
            doc.full_text is not None for doc in user_input.documents
        ):
            if not any(tool.read_only and tool.context_role == "artifact_read"
                       for tool in self._kernel.configuration.tools):
                raise ExecutionEngineError("native_user_document_reader_unavailable")
            run_id = str(uuid4())
            user_input = await capture_user_documents(
                user_input, artifacts=self._kernel.artifacts,
                scope=self._kernel.scope, run_id=run_id,
            )
            await self._check_activation_available()
        snapshot = self._kernel.start(
            run_id=run_id,
            goal=native_input_goal(request.input),
            session_id=request.session_id,
            origin={
                "spec_digest": pinned[1],
                "agent_revision_ref": compiled.spec.agent_revision_ref,
            },
            task_contract=task_contract,
            expected_parent_run_id=expected_parent_run_id,
            pin_parent=pin_parent,
            user_input=user_input,
            task_chain_id=task_chain_id,
        )
        return self._register_started_run(snapshot, compiled, pinned[1])

    async def fork(
        self,
        request: StartRequest,
        compiled: CompiledHarness,
        *,
        source_run_id: str,
        expected_source_revision: int,
        command_id: str | None = None,
        task_contract: TaskContract | None = None,
    ) -> RunHandle:
        """Explicit Native branch, with the same destination authority as start.

        This is a Native extension, not a new capability on every ExecutionEngine.
        The host chooses a new session and recompiles its current task contract.
        Neither request metadata nor an old specification grants execution rights.
        A stable command_id deduplicates retries. An existing local handle keeps
        its driver and cursor; a duplicate on another facade is observation only.
        """
        pinned = self.validate_start_request(request, compiled)
        if not isinstance(request.input, str):
            raise ExecutionEngineError("native fork input requires plain text")
        await self._check_activation_available()
        result = self._kernel.fork(
            source_run_id=source_run_id,
            expected_source_revision=expected_source_revision,
            command_id=command_id,
            goal=request.input,
            session_id=request.session_id,
            origin={
                "spec_digest": pinned[1],
                "agent_revision_ref": compiled.spec.agent_revision_ref,
            },
            task_contract=task_contract,
        )
        snapshot = result.snapshot
        if not result.created:
            existing = self._handles.get(snapshot.run_id)
            if existing is not None:
                return existing.model_copy(deep=True)
            self._fork_observers.add(snapshot.run_id)
            return self._register_started_run(snapshot, compiled, pinned[1], drive=False)
        return self._register_started_run(snapshot, compiled, pinned[1])

    def start_notification_turn(
        self,
        *,
        source_run_id: str,
        task_id: str,
        task_contract: TaskContract | None = None,
    ) -> NotificationTurnCreation:
        """Admit a background-notification turn from a completed ordinary run.

        Creates a new model-only, background-sourced turn in the source run's
        session — never a synthesized user goal. Inherits the source's goal (task
        continuity), background commands and task-chain budget. The kernel's
        ``command_probe`` delivers the observation at the next model boundary via
        the existing ``background_command_observation`` protocol. Idempotent: a
        repeat admission with the same request returns the already-created turn and
        re-attaches it so it can be driven; a different request for the same task_id
        is rejected by the store. Pre-checks (source terminal, task exists, not
        already notified, not canceled) run before creation. Does not revive a
        terminal run or reuse any unconfirmed effect.
        """
        return self._kernel.start_notification_turn(
            source_run_id=source_run_id, task_id=task_id,
            task_contract=task_contract,
        )

    def register_and_drive_notification_turn(
        self, result: NotificationTurnCreation, *, compiled: CompiledHarness, spec_digest: str,
    ) -> RunHandle:
        """Register a created notification turn and start driving it.

        The kernel creates and attaches the run; this registers the facade handle and
        starts the driver so the kernel advances through PREPARING_CONTEXT (where
        ``command_probe`` injects the observation) to the model boundary. Works for
        both a fresh creation and an idempotent repeat (re-attaches and re-drives).
        """
        return self._register_started_run(
            result.snapshot, compiled, spec_digest, drive=True,
        )

    def notified_task_ids(self, run_id: str) -> set[str]:
        """Authoritative set of task_ids already delivered in a run's ledger."""
        return self._kernel.notified_task_ids(run_id)

    def is_run_canceled(self, run_id: str) -> bool:
        """Authoritative terminal-status query; replaces event-store inference."""
        return self._kernel.is_run_canceled(run_id)

    def task_chain_remaining_budget(self, run_id: str) -> int | None:
        """Remaining model-call budget for the run's task chain (None = unbounded)."""
        return self._kernel.task_chain_remaining_budget(run_id)

    def task_chain_budget_status(self, run_id: str) -> dict:
        """Remaining budget across model/tool/wall-time (None = unbounded)."""
        return self._kernel.task_chain_budget_status(run_id)

    async def _check_activation_available(self) -> None:
        if self._activation_resource is not None:
            try:
                live = await self._activation_available()
            except Exception:
                raise ExecutionEngineError("activation resource unavailable") from None
            if live is not True:
                raise ExecutionEngineError("activation resource unavailable")

    def _register_started_run(
        self, snapshot: RunSnapshot, compiled: CompiledHarness, spec_digest: str,
        *, drive: bool = True,
    ) -> RunHandle:
        self._check_snapshot_binding(snapshot, compiled, spec_digest)
        handle = RunHandle(
            run_id=snapshot.run_id,
            session_id=snapshot.session_id,
            runtime_type="managed-native",
            native_ref={
                "configuration_digest": snapshot.configuration_digest,
                **snapshot.origin,
            },
        )
        self._handles[handle.run_id] = handle.model_copy(deep=True)
        self._cursors[handle.run_id] = 0
        self._changed[handle.run_id] = asyncio.Event()
        if drive:
            self._ensure_driver(handle.run_id)
        return handle

    @staticmethod
    def _check_snapshot_binding(
        snapshot: RunSnapshot, compiled: CompiledHarness, spec_digest: str,
    ) -> None:
        expected = {
            "spec_digest": spec_digest,
            "agent_revision_ref": compiled.spec.agent_revision_ref,
        }
        if snapshot.notification_command is not None:
            expected.update({
                "notification": True,
                "source_run_id": snapshot.notification_command.source_run_id,
                "task_id": snapshot.notification_command.task_id,
            })
        if snapshot.origin != expected:
            raise ExecutionEngineError("persisted run specification binding mismatch")

    def validate_start_request(
        self, request: StartRequest, compiled: CompiledHarness
    ) -> tuple[CompiledHarness, str]:
        """Check the pinned plan and request before host-owned contract compilation."""
        pinned = self._compiled.get(id(compiled))
        if pinned is None or pinned[0] is not compiled or pinned[1] != compiled.spec.content_hash():
            raise ExecutionEngineError("compiled plan is foreign or changed since compilation")
        scope = self._kernel.scope
        if request.user_id != scope.user_id or request.agent_id not in {None, scope.agent_id}:
            raise ExecutionEngineError("start identity does not match the host-authorized scope")
        # The host may bind a single trusted key ``source_run_id`` into the
        # request metadata for an explicit continuation. This is NOT an arbitrary
        # caller metadata override — only this one key is admitted, and the
        # adapter validates the source run (scope/session/terminal/cancel/chain)
        # before using it. All other metadata/config remain rejected.
        _admitted_metadata_keys = frozenset({"source_run_id"})
        extra_metadata = {
            key: value for key, value in (request.metadata or {}).items()
            if key not in _admitted_metadata_keys
        }
        if extra_metadata or request.config:
            raise ExecutionEngineError(
                "native start metadata/config are unsupported; scope comes from the host"
            )
        if request.model not in {None, self._profile_ref}:
            raise ExecutionEngineError("per-request model switching is unsupported")
        if isinstance(request.input, NativeUserInput):
            try:
                NativeUserInput.model_validate(request.input).check_profile(
                    self._kernel.configuration.profile
                )
            except ValueError as exc:
                reason = ("native_user_image_input_unsupported" if str(exc)
                          == "native_user_image_input_unsupported" else "native_user_input_invalid")
                raise ExecutionEngineError(reason) from None
        elif (not isinstance(request.input, str) or not request.input.strip()
              or len(request.input) > 1_048_576):
            raise ExecutionEngineError(
                "native input must be nonempty plain text within the message limit"
            )
        if not request.session_id.strip() or len(request.session_id) > 160:
            raise ExecutionEngineError(
                "native session_id must be nonempty and at most 160 characters"
            )
        return pinned

    async def attach(
        self, run_id: str, compiled: CompiledHarness, *, after_seq: int = 0, drive: bool = True
    ) -> RunHandle:
        """Restore from committed facts; never deserialize a foreign checkpoint.

        A running effect with no receipt is surfaced as uncertain by the kernel.
        Restoring a handle is not a claim that its external resources survived.
        """
        pinned = self._compiled.get(id(compiled))
        if pinned is None or pinned[0] is not compiled or pinned[1] != compiled.spec.content_hash():
            raise ExecutionEngineError("compiled plan is foreign or changed since compilation")
        snapshot = self._kernel.snapshot(run_id)
        self._check_snapshot_binding(snapshot, compiled, pinned[1])
        if type(after_seq) is not int or not 0 <= after_seq <= snapshot.event_seq:
            raise ExecutionEngineError("event cursor must address committed history")
        if run_id in self._handles:
            raise ExecutionEngineError("run already attached to this facade")
        handle = RunHandle(
            run_id=run_id,
            session_id=snapshot.session_id,
            runtime_type="managed-native",
            native_ref={"configuration_digest": snapshot.configuration_digest, **snapshot.origin},
        )
        if drive and not snapshot.phase.terminal:
            # Lease admission must happen before caching the facade handle.
            # A busy owner leaves this adapter retryable after expiry.
            self._kernel.attach(run_id)
            try:
                self._audit_restore(snapshot, self._kernel.fencing_token(run_id))
                await self._kernel.recover_activation_resources(
                    run_id, self._activation_resource, self._activation_available
                )
                await self._kernel.recover_browser_resources(run_id, self._browser_recovery)
            except BaseException:
                self._kernel.release(run_id)
                raise
        else:
            self._audit_restore(snapshot, None)
        self._handles[run_id] = handle.model_copy(deep=True)
        self._cursors[run_id] = after_seq
        self._changed[run_id] = asyncio.Event()
        if drive and not snapshot.phase.terminal:
            self._ensure_driver(run_id)
        return handle

    def _audit_restore(self, snapshot: RunSnapshot, fence: int | None) -> None:
        store = self._kernel.store
        if isinstance(store, RestoreAuditStore):
            store.record_restore(
                snapshot.scope,
                snapshot.run_id,
                reader_version=SNAPSHOT_READER_VERSION,
                configuration_digest=snapshot.configuration_digest,
                fencing_token=fence,
            )

    async def steer(
        self,
        handle: RunHandle,
        *,
        text: str,
        command_id: str,
        expected_goal_revision: int,
        task_contract: TaskContract | None = None,
    ):
        self._check_control(handle)
        receipt = self._kernel.steer(
            handle.run_id,
            text=text,
            command_id=command_id,
            expected_goal_revision=expected_goal_revision,
            task_contract=task_contract,
        )
        if receipt.status != "rejected":
            self._ensure_driver(handle.run_id)
        return receipt

    def reserved_goal_revision(self, handle: RunHandle) -> int:
        self._check(handle)
        return self._kernel.reserved_goal_revision(handle.run_id)

    async def child_tasks(self, handle: RunHandle) -> tuple[dict, ...]:
        self._check(handle)
        return self._kernel.child_tasks(handle.run_id)

    async def child_execution_view(self, handle: RunHandle, child_run_id: str) -> dict:
        self._check(handle)
        return self._kernel.child_execution_view(handle.run_id, child_run_id)

    async def cancel_child(self, handle: RunHandle, *, child_run_id: str, command_id: str):
        self._check_control(handle)
        try:
            receipt = self._kernel.cancel_child(handle.run_id, child_run_id=child_run_id,
                                                command_id=command_id)
        except StoreConflict as exc:
            raise ValueError("child_cancellation_command_conflict") from exc
        if receipt.status != "rejected":
            self._ensure_driver(handle.run_id)
        return receipt

    async def promote_child(self, handle: RunHandle, *, child_run_id: str, command_id: str):
        self._check_control(handle)
        receipt = self._kernel.promote_child(handle.run_id, child_run_id=child_run_id,
                                             command_id=command_id)
        if receipt.status != "rejected":
            self._ensure_driver(handle.run_id)
        return receipt

    async def send_child_input(self, handle: RunHandle, *, child_run_id: str, text: str,
                               command_id: str, expected_goal_revision: int):
        self._check_control(handle)
        try:
            receipt = self._kernel.send_child_input(
                handle.run_id, child_run_id=child_run_id, text=text,
                command_id=command_id, expected_goal_revision=expected_goal_revision,
            )
        except StoreConflict as exc:
            raise ValueError("child_input_command_conflict") from exc
        if receipt.status != "rejected":
            self._ensure_driver(handle.run_id)
        return receipt

    async def guide(self, handle: RunHandle, *, text: str, command_id: str,
                    expected_goal_revision: int):
        self._check_control(handle)
        receipt = self._kernel.guide(handle.run_id, text=text, command_id=command_id,
                                    expected_goal_revision=expected_goal_revision)
        if receipt.status != "rejected":
            self._ensure_driver(handle.run_id)
        return receipt

    def _check(self, handle: RunHandle) -> RunSnapshot:
        if self._handles.get(handle.run_id) != handle:
            raise ExecutionEngineError("unknown or altered native handle for this engine instance")
        return self._kernel.snapshot(handle.run_id)

    def _check_control(self, handle: RunHandle) -> RunSnapshot:
        snapshot = self._check(handle)
        if handle.run_id in self._fork_observers:
            raise ExecutionEngineError(
                "duplicate fork handle permits observation only; "
                "explicitly attach with a new facade to drive"
            )
        if handle.run_id in self._closed:
            raise ExecutionEngineError(
                "closed native handle permits observation only; "
                "explicitly attach with a new facade to drive"
            )
        if handle.run_id in self._releases:
            raise ExecutionEngineError(
                "closing native handle permits observation only; "
                "await its release before attaching with a new facade"
            )
        if (ownership := self._ownership.get(handle.run_id)) and ownership[1].done():
            self._driver_result(ownership[1])
        return snapshot

    def _ensure_driver(self, run_id: str) -> None:
        ownership = self._ownership.get(run_id)
        if ownership is not None and ownership[1].done():
            self._driver_result(ownership[1])
        self._kernel.attach(run_id)
        previous = self._drivers.get(run_id)
        if previous is None or previous.done():
            if (ownership is None and self._kernel.leased_ownership
                    and not self._kernel.snapshot(run_id).phase.terminal):
                released = asyncio.Event()
                keeper = asyncio.create_task(self._retain_ownership(run_id, released))
                keeper.add_done_callback(
                    lambda completed: completed.exception() if not completed.cancelled() else None
                )
                self._ownership[run_id] = (released, keeper)
            from ksadk.harness.public_output import current_public_output_sink, public_output_scope

            sink = current_public_output_sink()
            if sink is not None:
                self._public_output_sinks[run_id] = sink
            with public_output_scope(self._public_output_sinks.get(run_id)):
                task = asyncio.create_task(self._drive(run_id))
            # Retrieve exceptions even if a client disconnects; stream/control
            # still observes task.result(), and native facts are never replaced.
            task.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
            self._drivers[run_id] = task

    async def _retain_ownership(self, run_id: str, released: asyncio.Event) -> None:
        try:
            await self._kernel.retain_ownership(run_id, released)
        except Exception as error:
            raise driver_failure(error, snapshot=self._kernel.snapshot(run_id)) from None
        finally:
            self._changed[run_id].set()

    async def _drive(self, run_id: str) -> None:
        terminal = False
        try:
            while True:
                before = self._kernel.snapshot(run_id)
                after = await self._kernel.advance(run_id)
                self._changed[run_id].set()
                terminal = after.phase.terminal
                if after.phase.terminal or after.revision == before.revision:
                    return
                await asyncio.sleep(0)
        except Exception as error:
            # Observe committed facts only. A lost fence or failed Store write
            # cannot authorize this host driver to manufacture a terminal.
            snapshot, effect_kind = None, None
            try:
                snapshot = self._kernel.snapshot(run_id)
                if snapshot.pending_intent is not None:
                    intent = self._kernel.store.get_intent(
                        snapshot.scope, run_id, snapshot.pending_intent
                    )
                    effect_kind = intent.kind if intent is not None else None
            except Exception:
                pass
            if ownership := self._ownership.get(run_id):
                ownership[0].set()
            raise driver_failure(error, snapshot=snapshot, effect_kind=effect_kind) from None
        finally:
            if terminal and (ownership := self._ownership.get(run_id)):
                ownership[0].set()
            self._changed[run_id].set()

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        self._check(handle)
        run_id = handle.run_id
        if run_id in self._streaming:
            raise ExecutionEngineError("only one active event consumer is allowed per native run")
        self._streaming.add(run_id)
        try:
            while True:
                changed = self._changed[run_id]
                changed.clear()
                for native in self._kernel.events(run_id, after_seq=self._cursors[run_id]):
                    projected = self._project_event(native, handle)
                    self._cursors[run_id] = native.seq
                    yield projected
                snapshot = self._check(handle)
                if self._cursors[run_id] < snapshot.event_seq:
                    continue
                driver = self._drivers.get(run_id)
                if driver is None or driver.done():
                    if (ownership := self._ownership.get(run_id)) and ownership[1].done():
                        self._driver_result(ownership[1])
                    if driver is not None:
                        self._driver_result(driver)
                    return
                if snapshot.phase.terminal:
                    return
                # Deltas may commit while a model effect is still in flight.
                # Refresh committed facts without coupling the kernel to a UI.
                try:
                    await asyncio.wait_for(changed.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._streaming.discard(run_id)

    @staticmethod
    def _driver_result(driver: asyncio.Task[None]) -> None:
        try:
            driver.result()
        except NativeDriverFailure as error:
            raise error from None
        except (Exception, asyncio.CancelledError) as error:
            raise driver_failure(error) from None

    def _project_event(self, native: NativeEvent, handle: RunHandle) -> RuntimeEvent:
        payload = {
            **native.payload,
            "native_event_kind": native.kind,
            "timestamp_source": "unavailable",
            "turn_id": native.turn_id,
        }
        phase = None
        if native.kind == "text.completed":
            kind, phase = EventType.TEXT_COMPLETED, "final_answer"
        elif native.kind == "text.delta":
            kind, phase = EventType.TEXT_DELTA, "commentary"
            payload["provisional"] = True
        elif native.kind in _TERMINALS:
            kind = _TERMINALS[native.kind]
            payload["status"] = native.kind.removeprefix("run.")
            if kind == EventType.RUN_FAILED:
                payload["error"] = payload.get("reason", "native_run_failed")
        elif native.kind == "run.started":
            kind, payload["status"] = EventType.RUN_STARTED, "running"
        elif native.kind == "usage.reported":
            kind = EventType.USAGE_REPORTED
            payload["total_tokens"] = int(payload["input_tokens"]) + int(payload["output_tokens"])
            payload["cached_tokens"] = payload.pop("cached_input_tokens", 0)
        elif native.kind in {EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED}:
            kind = native.kind
        else:
            kind, payload["status"] = EventType.RUN_PROGRESS, "running"
            if (
                native.kind == "command.consumed"
                and payload.get("kind") == "pause"
                and payload.get("reason") in {"applied", "already_paused"}
            ):
                payload["status"] = "paused"
        scope = self._kernel.scope
        return RuntimeEvent.create(
            kind,
            agent_id=scope.agent_id,
            user_id=scope.user_id,
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            run_id=handle.run_id,
            scope_id=f"agent:{scope.agent_id}",
            seq_id=native.seq,
            event_id=native.event_id,
            timestamp=0,
            phase=phase,
            payload=payload,
        )

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self._check_control(handle)
        return await self._cancel_run(handle)

    async def _cancel_run(self, handle: RunHandle) -> CancelResult:
        """Stop after admission, also used by the facade-owned release task."""
        if self._check(handle).phase.terminal:
            return CancelResult.NOT_RUNNING
        self._kernel.submit(handle.run_id, RunCommand(command_id=str(uuid4()), kind="cancel"))
        self._ensure_driver(handle.run_id)
        await asyncio.wait({self._drivers[handle.run_id]})
        self._driver_result(self._drivers[handle.run_id])
        return (
            CancelResult.INTERRUPTED_ACTIVE_TURN
            if self._check(handle).phase == RunPhase.CANCELED
            else CancelResult.NOT_RUNNING
        )

    async def pause(self, handle: RunHandle) -> PauseResult:
        if self._check_control(handle).phase.terminal:
            return PauseResult.NOT_RUNNING
        self._kernel.submit(handle.run_id, RunCommand(command_id=str(uuid4()), kind="pause"))
        self._ensure_driver(handle.run_id)
        await asyncio.wait({self._drivers[handle.run_id]})
        self._driver_result(self._drivers[handle.run_id])
        return (
            PauseResult.PAUSED_ACTIVE_TURN
            if self._check(handle).phase == RunPhase.PAUSED
            else PauseResult.NOT_RUNNING
        )

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        snapshot = self._check_control(handle)
        if (
            target.kind != "invocation_id"
            or target.id != handle.run_id
        ):
            raise ExecutionEngineError(
                "native resume requires the exact open invocation; "
                "checkpoint/thread resume unsupported"
            )
        if snapshot.phase == RunPhase.PAUSED and payload is None:
            command = RunCommand(command_id=str(uuid4()), kind="resume")
        elif snapshot.phase == RunPhase.AWAITING_INTERACTION:
            command = self._approval_command(snapshot, payload)
        else:
            raise ExecutionEngineError(
                "native resume supports only a paused run or its pending approval"
            )
        self._kernel.submit(handle.run_id, command)
        self._ensure_driver(handle.run_id)
        return handle.model_copy(deep=True)

    @staticmethod
    def _approval_command(snapshot: RunSnapshot, payload: ResumePayload | None) -> RunCommand:
        pending_call_id = (
            snapshot.child_approval.tool_call.call_id if snapshot.child_approval is not None
            else snapshot.pending_tools[0]["call_id"] if snapshot.pending_tools else None
        )
        if (
            payload is None
            or payload.kind != "approval_decision"
            or payload.session_context is not None
            or not isinstance(payload.data, dict)
            or set(payload.data) != {"approval_id", "decision"}
            or payload.data["approval_id"] != snapshot.pending_interaction
            or not isinstance(payload.data["decision"], str)
            or payload.data["decision"] not in {"allow", "deny"}
            or pending_call_id is None
            or payload.call_id != pending_call_id
        ):
            raise ExecutionEngineError(
                "approval must match the pending call/approval IDs and an allow/deny decision"
            )
        return RunCommand(
            command_id=str(uuid4()),
            kind="submit_interaction",
            payload={
                "interaction_id": snapshot.pending_interaction,
                "decision": payload.data["decision"],
            },
        )

    async def snapshot_state(self, handle: RunHandle) -> HarnessState:
        """Compatibility view only; long display strings are bounded, never persisted."""
        native = self._check(handle)
        events = self._kernel.events(handle.run_id)
        messages = [
            Message(
                role=item["role"],
                content=str(item.get("content") or "")[:1_048_576],
                tool_call_id=item.get("tool_call_id"),
            )
            for item in native.messages
            # The Native transcript also contains host-authored model recovery,
            # compaction and verification prompts. They belong to the model
            # context, not the public HarnessState conversation. Real user
            # messages have no kind; tool receipts remain observable.
            if item.get("role") == "tool" or item.get("kind") is None
        ]
        for event in events:
            if event.kind == "text.completed":
                messages.append(
                    Message(role="assistant", content=str(event.payload["text"])[:1_048_576])
                )
        pending = [
            ToolCall(
                call_id=str(item["call_id"]), name=str(item["name"]), arguments=item["arguments"]
            )
            for item in native.pending_tools
        ]
        if native.pending_interaction and native.child_approval is not None:
            call = native.child_approval.tool_call
            pending = [ToolCall(call_id=call.call_id, name=call.name, arguments=call.arguments),
                       *pending]
        approval = None
        if native.pending_interaction and pending:
            approval = ApprovalRequest(
                approval_id=native.pending_interaction,
                call_id=pending[0].call_id,
                reason="Host tool approval is required.",
                requested_at=0,
            )
        return HarnessState(
            tenant_id=native.scope.tenant_id,
            user_id=native.scope.user_id,
            agent_id=native.scope.agent_id,
            session_id=native.session_id,
            run_id=native.run_id,
            messages=messages,
            working_context=WorkingContext(goal=native.goal[:8192]),
            pending_tool_calls=pending,
            pending_approval=approval,
            turn_count=_logical_turn_count(events),
            status=_status(native.phase),
            capability_snapshot=CapabilitySnapshot(entries=("native:committed-event-stream",)),
        )

    def capabilities(self) -> EngineCapabilityMatrix:
        return EngineCapabilityMatrix(
            cancel=EngineCapability(
                True, "cancels the run and cooperatively cancels active host effects"
            ),
            resume=EngineCapability(
                True,
                "pause/approval restore with matching scope/config/spec; uncertain effects stop",
            ),
            checkpoint=EngineCapability(False, "checkpoint/time-travel facade is not implemented"),
            interrupt=EngineCapability(False, "explicit terminal interrupt is not implemented"),
            durable_across_process=EngineCapability(
                False, "persisted records do not implement cross-process effect recovery"
            ),
            streaming=EngineCapability(
                True, "committed events; provisional token deltas for pinned streaming profiles"
            ),
        )

    async def close(self, handle: RunHandle) -> None:
        await self._release_handle(handle, stop=True)

    async def detach(self, handle: RunHandle) -> None:
        """Release an idle durable suspension without changing its ledger phase."""
        await self._release_handle(handle, stop=False)

    async def _release_handle(self, handle: RunHandle, *, stop: bool) -> None:
        snapshot = self._check(handle)
        if handle.run_id in self._closed:
            return
        if handle.run_id in self._fork_observers:
            self._closed.add(handle.run_id)
            return
        release = self._releases.get(handle.run_id)
        if release is None:
            if not stop and snapshot.phase not in {
                RunPhase.PAUSED,
                RunPhase.AWAITING_INTERACTION,
                RunPhase.COMPLETED,
                RunPhase.FAILED,
                RunPhase.CANCELED,
                RunPhase.INTERRUPTED,
            }:
                raise ExecutionEngineError("only an idle Native run can be detached")
            # Register before yielding: later controls cannot enter the inbox,
            # and another close/detach joins this exact release disposition.
            release = asyncio.create_task(
                self._release_run(handle.model_copy(deep=True), stop=stop)
            )
            release.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
            self._releases[handle.run_id] = release
        # An RPC cancellation cannot abandon the accepted stop or revoke its
        # cleanup ownership. A failed release stays observable and fail-closed.
        await asyncio.shield(release)

    async def _release_run(self, handle: RunHandle, *, stop: bool) -> None:
        try:
            if stop:
                await self._cancel_run(handle)
            else:
                driver = self._drivers.get(handle.run_id)
                if driver is not None:
                    await driver
                    self._driver_result(driver)
        finally:
            try:
                if stop:
                    # Scope owns child execution even when parent cancellation
                    # fails or its terminal ledger is already immutable. Preserve
                    # the original failure after actual resource cleanup.
                    await self._kernel.release_children(handle.run_id)
            finally:
                if ownership := self._ownership.get(handle.run_id):
                    ownership[0].set()
                    await ownership[1]
        self._kernel.release(handle.run_id)
        self._closed.add(handle.run_id)
        self._public_output_sinks.pop(handle.run_id, None)

    def release_notification_lease(self, source_run_id: str, task_id: str) -> None:
        """Release a stale durable lease for an admitted-but-not-driven turn.

        A notification turn admitted via ``start_notification_turn`` attaches the
        run (acquiring its durable lease) before the driver is scheduled. If the
        process exits in that crash window, the lease is orphaned and a restart
        cannot re-drive the turn until the lease TTL elapses. The wakeup
        coordinator calls this on service shutdown for admitted watches so a
        restart can re-drive promptly. If the run still has an active driver, it
        is left untouched — the activation close owns its disposal. Idempotent.
        """
        from .native.notification_admission import notification_run_id

        run_id = notification_run_id(self._kernel.scope, source_run_id, task_id)
        driver = self._drivers.get(run_id)
        if driver is not None and not driver.done():
            return  # actively driven; activation close will dispose it
        # Admission registers a handle before scheduling its first driver.
        # Sealing that never-started local handle prevents activation disposal
        # from turning a recoverable durable admission into a cancellation.
        # A driven turn (including a failed driver) keeps ordinary close rules.
        unstarted = (
            run_id in self._handles and driver is None
            and run_id not in self._ownership
            and self._kernel.snapshot(run_id).phase == RunPhase.CREATED
        )
        self._kernel.release(run_id)
        if unstarted:
            self._closed.add(run_id)


def _status(phase: RunPhase) -> RunStatus:
    return {
        RunPhase.CREATED: RunStatus.PENDING,
        RunPhase.PAUSED: RunStatus.PAUSED,
        RunPhase.AWAITING_INTERACTION: RunStatus.AWAITING_APPROVAL,
        RunPhase.COMPACTING: RunStatus.COMPACTING,
        RunPhase.COMPLETED: RunStatus.COMPLETED,
        RunPhase.FAILED: RunStatus.FAILED,
        RunPhase.INTERRUPTED: RunStatus.FAILED,
        RunPhase.CANCELED: RunStatus.CANCELED,
    }.get(phase, RunStatus.RUNNING)
