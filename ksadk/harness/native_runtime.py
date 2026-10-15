"""Opt-in Native RuntimeAdapter; canonical projection without LangGraph imports.

The host owns the configured engine, authenticated scope, ports and store.
Only Native ledger facts define history, interaction, usage and completion.
Injected activation close callbacks run after accepted adapter operations exit;
registry/provider ownership and plugin installation remain with the host.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ksadk.events.reducer import StreamReducer
from ksadk.harness.native.adapters import _snapshot
from ksadk.harness.native.completion import TaskContract
from ksadk.harness.native.ports import RunConfiguration, digest
from ksadk.harness.native.state import RunPhase, RunScope, RunSnapshot
from ksadk.harness.native.studio_admission import StudioAdmission, studio_mapping_digest
from ksadk.harness.native.user_input import native_input_goal
from ksadk.harness.native_facade import NativeHarnessEngine
from ksadk.harness.native_projection import NativeEventProjection
from ksadk.harness.spec import HarnessSpec
from ksadk.harness.stores.native_contract import RunStore
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix, SteerPayload
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runtime import (
    BaseRuntime,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)


class NativeRuntime(BaseRuntime):
    runtime_type = "harness"

    def __init__(self, *, durable: bool) -> None:
        self._durable = durable

    def native_capabilities(self) -> dict:
        return {
            "cancel": {"supported": True},
            "resume": {"supported": True},
            "checkpoint": {"supported": False},
            "session_continuity": {"durable": self._durable, "scope": "host"},
        }


@dataclass(frozen=True)
class TaskContractInput:
    """Host-visible facts for compiling one turn's acceptance contract."""

    goal: str
    session_id: str
    parent_run_id: str | None
    parent_goal: str | None
    parent_contract: TaskContract | None


class NativeRuntimeAdapter(RuntimeAdapter):
    """Map one pinned Native engine onto the existing public Harness runtime.

    Start accepts plain text and the authorized user/agent identity. Arbitrary
    metadata, history, model and policy overrides remain explicitly rejected.
    Host assembly must resolve those concerns before constructing this adapter.
    """

    def __init__(
        self,
        spec: HarnessSpec,
        *,
        engine: NativeHarnessEngine,
        scope: RunScope,
        configuration: RunConfiguration,
        store: RunStore,
        durable: bool = False,
        studio_admission: StudioAdmission | None = None,
        task_contract_factory: (
            Callable[[TaskContractInput], TaskContract | Awaitable[TaskContract]] | None
        ) = None,
        turn_completed_consumer: Callable[[RunSnapshot], Awaitable[None]] | None = None,
        close_callbacks: tuple[Callable[[], Awaitable[None]], ...] = (),
    ) -> None:
        super().__init__(NativeRuntime(durable=durable))
        self._spec = spec.model_copy(deep=True)
        self._engine = engine
        self._scope = _snapshot(scope)
        self._configuration = _snapshot(configuration)
        self._store, self._durable = store, durable
        self._studio_admission = _snapshot(studio_admission) if studio_admission else None
        if task_contract_factory is not None and not callable(task_contract_factory):
            raise TypeError("Native task contract factory must be callable")
        self._task_contract_factory = task_contract_factory
        if turn_completed_consumer is not None and not callable(turn_completed_consumer):
            raise TypeError("Native completed-Turn consumer must be callable")
        self._turn_completed_consumer = turn_completed_consumer
        if any(not callable(close) for close in close_callbacks):
            raise TypeError("Native close callbacks must be callable")
        self._close_callbacks = list(close_callbacks)
        self._compiled = None
        self._compile_lock = asyncio.Lock()
        self._handles: dict[str, RunHandle] = {}
        self._delivery: dict[str, int] = {}
        self._streaming: set[str] = set()
        self._closed: set[str] = set()
        self._admissions: set[asyncio.Task[RunHandle]] = set()
        self._closes: dict[str, asyncio.Task[None]] = {}
        self._shutdown: asyncio.Task[None] | None = None

    def _check_activation_open(self) -> None:
        if self._shutdown is not None:
            raise ValueError("Native adapter activation is closing or closed")

    def _admission_done(self, task: asyncio.Task[RunHandle]) -> None:
        self._admissions.discard(task)
        if not task.cancelled():
            task.exception()

    async def _admit(self, operation: Awaitable[RunHandle]) -> RunHandle:
        task = asyncio.create_task(operation)
        self._admissions.add(task)
        task.add_done_callback(self._admission_done)
        # Startup is caller-cancellable. Shutdown also owns its identity until
        # it exits, even if its original caller no longer awaits it.
        return await task

    @property
    def harness_spec(self) -> HarnessSpec:
        return self._spec.model_copy(deep=True)

    def session_parent_run_id(self, session_id: str) -> str | None:
        """Expose the scoped ledger head for a host-owned conversation handoff."""
        head = self._store.session_head(self._scope, session_id)
        return None if head is None else head.run_id

    def has_pending_session_work(self, session_id: str) -> bool:
        """Read lifecycle ownership before a host releases an activation.

        A terminal ledger alone is insufficient while its owned close task
        still consumes Memory or releases tools. Durable suspended heads also
        remain live after this adapter has detached their local handle.
        """
        if any(not task.done() for task in self._admissions):
            return True
        if self._shutdown is not None and not self._shutdown.done():
            return True
        head = self._store.session_head(self._scope, session_id)
        if head is not None and not head.phase.terminal:
            return True
        return any(
            handle.session_id == session_id and run_id not in self._closed
            for run_id, handle in self._handles.items()
        )

    def can_replace_session_activation(self, session_id: str) -> bool:
        """Permit option rebinding only after an owned turn has fully closed."""
        head = self._store.session_head(self._scope, session_id)
        return (head is not None and head.phase.terminal
                and not self.has_pending_session_work(session_id))

    def admit_studio_build(
        self,
        *,
        build_id: str,
        manifest_sha256: str,
        model: str | None,
        request_config: Mapping[str, Any],
        project_dir: Path | None = None,
        launch_config: Mapping[str, Any] | None = None,
        require_pin: bool = False,
    ) -> None:
        """Reject unbound overrides before Studio removes host-only StartRequest fields."""
        admission = self._studio_admission
        if admission is None:
            if require_pin:
                raise ValueError("native_studio_build_unbound")
            if request_config:
                raise ValueError("native_studio_request_config_unbound")
            if model not in {None, self._spec.model.profile_ref}:
                raise ValueError("native_studio_model_unbound")
            return
        if build_id != admission.build_id or manifest_sha256 != admission.manifest_sha256:
            raise ValueError("native_studio_build_unbound")
        if model != admission.model:
            raise ValueError("native_studio_model_unbound")
        if studio_mapping_digest(request_config) != admission.request_config_digest:
            raise ValueError("native_studio_request_config_unbound")
        if project_dir is not None and str(project_dir.resolve()) != admission.project_dir:
            raise ValueError("native_studio_launch_config_unbound")
        if launch_config is not None and (
            studio_mapping_digest(launch_config) != admission.launch_config_digest
        ):
            raise ValueError("native_studio_launch_config_unbound")

    async def _ensure_compiled(self):
        async with self._compile_lock:
            if self._compiled is None:
                self._compiled = await self._engine.compile(self._spec)
        return self._compiled

    async def preflight(self) -> None:
        self._check_activation_open()
        await self._ensure_compiled()
        self._check_activation_open()

    def _external(self, internal: RunHandle) -> RunHandle:
        return internal.model_copy(
            deep=True,
            update={
                "runtime_type": "harness",
                "native_ref": {
                    **internal.native_ref,
                    "engine_kind": "native",
                    "scope_digest": digest(self._scope),
                },
            },
        )

    def snapshot(self, handle: RunHandle) -> RunSnapshot:
        if handle.runtime_type != "harness" or handle.native_ref.get("engine_kind") != "native":
            raise ValueError("handle does not name the pinned Native Harness engine")
        if handle.native_ref.get("scope_digest") != digest(self._scope):
            raise ValueError("handle authorization scope mismatch")
        state = self._store.load(self._scope, handle.run_id)
        expected = {
            "configuration_digest": state.configuration_digest,
            **state.origin,
            "engine_kind": "native",
            "scope_digest": digest(self._scope),
        }
        if (
            state.session_id != handle.session_id
            or handle.native_ref != expected
            or state.configuration_digest != self._configuration.digest
            or state.origin.get("spec_digest") != self._spec.content_hash()
        ):
            raise ValueError("handle or immutable run configuration changed")
        return state.model_copy(deep=True)

    def _internal(self, handle: RunHandle) -> RunHandle:
        internal = self._admitted_internal(handle)
        self._check_activation_open()
        if handle.run_id in self._closes:
            raise ValueError("Native adapter handle is closing")
        return internal

    def _admitted_internal(self, handle: RunHandle) -> RunHandle:
        """Validate immutable admission, including for already accepted cleanup."""
        self.snapshot(handle)
        if handle.run_id in self._closed or handle.run_id not in self._handles:
            raise ValueError("Native run is not attached to this adapter")
        internal = self._handles[handle.run_id]
        if self._external(internal) != handle:
            raise ValueError("Native handle changed since admission")
        return internal.model_copy(deep=True)

    async def start(self, request: StartRequest) -> RunHandle:
        self._check_activation_open()
        return await self._admit(self._start(request))

    async def _start(self, request: StartRequest) -> RunHandle:
        compiled = await self._ensure_compiled()
        self._check_activation_open()
        self._engine.validate_start_request(request, compiled)
        contract = None
        parent_run_id = None
        if self._task_contract_factory is not None:
            parent = self._store.session_head(self._scope, request.session_id)
            parent_run_id = parent.run_id if parent else None
            parent_contract = (
                TaskContract.model_validate(parent.task_contract)
                if parent is not None and parent.task_contract is not None
                else None
            )
            contract = self._task_contract_factory(
                TaskContractInput(
                    goal=native_input_goal(request.input),
                    session_id=request.session_id,
                    parent_run_id=parent_run_id,
                    parent_goal=parent.goal if parent else None,
                    parent_contract=parent_contract,
                )
            )
            if inspect.isawaitable(contract):
                contract = await contract
            if not isinstance(contract, TaskContract):
                raise ValueError("native_task_contract_invalid")
        self._check_activation_open()
        internal = await self._engine.start(
            request,
            compiled,
            task_contract=contract,
            expected_parent_run_id=parent_run_id,
            pin_parent=self._task_contract_factory is not None,
        )
        return await self._finish_admission(internal)

    async def _finish_admission(self, internal: RunHandle) -> RunHandle:
        """Account for late host returns before refusing a stopped activation."""
        external = self._external(internal)
        self.snapshot(external)
        self._handles[internal.run_id] = internal.model_copy(deep=True)
        self._delivery[internal.run_id] = 0
        if self._shutdown is not None:
            # A host may settle despite cancellation. Keep its real identity,
            # join release, and never return a usable late handle to the caller.
            await self.close(external)
            self._check_activation_open()
        return external

    def replay(self, handle: RunHandle, *, after_seq: int = 0) -> tuple:
        """Read canonical history, independent of the live delivery cursor."""
        self.snapshot(handle)
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("canonical replay cursor must be nonnegative")
        projection = NativeEventProjection(self._scope, handle.run_id)
        events = tuple(
            event
            for native in self._store.events(self._scope, handle.run_id)
            for event in projection.consume(native)
        )
        if after_seq > projection.seq:
            raise ValueError("canonical replay cursor is beyond committed history")
        return tuple(event for event in events if event.seq > after_seq)

    def projection(self, handle: RunHandle):
        reducer = StreamReducer()
        for event in self.replay(handle):
            reducer.apply(event)
        return reducer.snapshot()

    async def stream(self, handle: RunHandle):
        internal = self._admitted_internal(handle)
        if handle.run_id in self._streaming:
            raise ValueError("only one live canonical consumer is allowed per run")
        self._streaming.add(handle.run_id)
        projection = NativeEventProjection(self._scope, handle.run_id)

        def committed():
            self.snapshot(handle)
            return tuple(
                event
                for native in self._store.events(
                    self._scope, handle.run_id, after_seq=projection.native_seq
                )
                for event in projection.consume(native)
                if event.seq > self._delivery[handle.run_id]
            )

        try:
            # Rebuild from the ledger so a consumer disconnect between two
            # canonical mutations never loses the rest of a native event.
            for event in committed():
                self._delivery[handle.run_id] = event.seq
                yield event
            async for _ in self._engine.stream(internal):
                for event in committed():
                    self._delivery[handle.run_id] = event.seq
                    yield event
        finally:
            self._streaming.discard(handle.run_id)

    async def attach(self, handle: RunHandle) -> RunHandle:
        self.snapshot(handle)
        self._check_activation_open()
        return await self._admit(self._attach(handle))

    async def _attach(self, handle: RunHandle) -> RunHandle:
        self.snapshot(handle)
        if handle.run_id in self._handles and handle.run_id not in self._closed:
            self._internal(handle)
            return handle.model_copy(deep=True)
        if handle.run_id in self._closed:
            raise ValueError("closed Native adapter handle cannot be reattached")
        compiled = await self._ensure_compiled()
        self._check_activation_open()
        internal = await self._engine.attach(handle.run_id, compiled)
        if self._external(internal) != handle:
            raise ValueError("restored Native handle differs from persisted pins")
        return await self._finish_admission(internal)

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        if not self._durable:
            raise UnsupportedControlError("native_durable_store_unavailable")
        return await self.attach(handle)

    def is_handle_attached(self, handle: RunHandle) -> bool:
        try:
            self._internal(handle)
            return True
        except (ValueError, KeyError, RuntimeError):
            return False

    async def cancel(self, handle: RunHandle):
        return await self._engine.cancel(self._internal(handle))

    async def pause(self, handle: RunHandle):
        return await self._engine.pause(self._internal(handle))

    async def resume(
        self, handle: RunHandle, target: ResumeTarget, payload: ResumePayload | None
    ) -> RunHandle:
        internal = self._internal(handle)
        if payload is not None and payload.kind == "approval_decision":
            snapshot = self.snapshot(handle)
            data = payload.data
            decision = data.get("decision") if isinstance(data, dict) else data
            normalized = {
                "allow": "allow",
                "approve": "allow",
                "approved": "allow",
                "deny": "deny",
                "reject": "deny",
                "rejected": "deny",
                "cancel": "deny",
                "canceled": "deny",
            }.get(decision if isinstance(decision, str) else "")
            if normalized is None:
                raise ValueError("unsupported Native approval decision")
            approval_id = (
                data.get("approval_id", snapshot.pending_interaction)
                if isinstance(data, dict)
                else snapshot.pending_interaction
            )
            payload = payload.model_copy(
                deep=True, update={"data": {"approval_id": approval_id, "decision": normalized}}
            )
        restored = await self._engine.resume(internal, target, payload)
        self._handles[handle.run_id] = restored.model_copy(deep=True)
        return self._external(restored)

    async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
        await self.resume(handle, ResumeTarget(kind="invocation_id", id=handle.run_id), payload)

    async def steer(self, handle: RunHandle, payload: SteerPayload):
        if not isinstance(payload, SteerPayload) or payload.run_id not in {None, handle.run_id}:
            raise ValueError("steering must target this Native run")
        if type(payload.content) is not str or not payload.content.strip():
            raise ValueError("Native steering requires plain text")
        # Several corrections may arrive while one model effect is in flight.
        # Reserve their CAS revisions in command order instead of reading the
        # same committed snapshot revision for every queued instruction.
        expected_revision = self._engine.reserved_goal_revision(self._internal(handle))
        receipt = await self._engine.steer(
            self._internal(handle),
            text=payload.content,
            command_id=str(uuid4()),
            expected_goal_revision=expected_revision,
        )
        if receipt.status == "rejected":
            raise ValueError("Native steering command was rejected")
        return receipt

    async def guide(self, handle: RunHandle, payload: SteerPayload):
        if not isinstance(payload, SteerPayload) or payload.run_id not in {None, handle.run_id}:
            raise ValueError("guidance must target this Native run")
        if type(payload.content) is not str or not payload.content.strip():
            raise ValueError("Native guidance requires plain text")
        internal = self._internal(handle)
        receipt = await self._engine.guide(
            internal, text=payload.content, command_id=str(uuid4()),
            expected_goal_revision=self.snapshot(handle).goal_revision,
        )
        if receipt.status == "rejected":
            raise ValueError("Native guidance command was rejected")
        return receipt

    async def follow_up(self, handle: RunHandle, text: str) -> RunHandle:
        self._internal(handle)
        if not self.snapshot(handle).phase.terminal:
            raise ValueError("follow_up requires a terminal Native turn")
        return await self.start(
            StartRequest(
                input=text,
                user_id=self._scope.user_id,
                agent_id=self._scope.agent_id,
                session_id=handle.session_id,
            )
        )

    async def child_tasks(self, handle: RunHandle) -> tuple[dict, ...]:
        return await self._engine.child_tasks(self._internal(handle))

    async def child_execution_view(self, handle: RunHandle, child_run_id: str) -> dict:
        return await self._engine.child_execution_view(self._internal(handle), child_run_id)

    async def cancel_child(self, handle: RunHandle, *, child_run_id: str, command_id: str):
        return await self._engine.cancel_child(self._internal(handle), child_run_id=child_run_id,
                                               command_id=command_id)

    async def promote_child(self, handle: RunHandle, *, child_run_id: str, command_id: str):
        return await self._engine.promote_child(self._internal(handle), child_run_id=child_run_id,
                                                command_id=command_id)

    async def snapshot_state(self, handle: RunHandle):
        return await self._engine.snapshot_state(self._admitted_internal(handle))

    async def send_child_input(self, handle: RunHandle, *, child_run_id: str, text: str,
                               command_id: str, expected_goal_revision: int):
        return await self._engine.send_child_input(
            self._internal(handle), child_run_id=child_run_id, text=text,
            command_id=command_id, expected_goal_revision=expected_goal_revision,
        )

    async def checkpoint(self, handle: RunHandle):
        self._internal(handle)
        raise UnsupportedControlError("native_checkpoint_time_travel_unavailable")

    async def close(self, handle: RunHandle) -> None:
        if handle.run_id in self._closed:
            self.snapshot(handle)
            return
        internal = self._admitted_internal(handle)
        close = self._closes.get(handle.run_id)
        if close is None:
            snapshot = self.snapshot(handle)
            detach = self._durable and snapshot.phase in {
                RunPhase.PAUSED, RunPhase.AWAITING_INTERACTION,
            }
            close = asyncio.create_task(self._close_run(internal, detach=detach))
            close.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
            self._closes[handle.run_id] = close
        await asyncio.shield(close)

    async def _close_run(self, internal: RunHandle, *, detach: bool) -> None:
        if detach:
            # RuntimeExecutor.close releases this process's adapter. A durable
            # suspension belongs to the ledger and must survive Studio's
            # graceful shutdown; explicit cancel remains a separate control.
            await self._engine.detach(internal)
        else:
            await self._engine.close(internal)
        try:
            if self._turn_completed_consumer is not None:
                snapshot = self._store.load(self._scope, internal.run_id)
                if snapshot.phase == RunPhase.COMPLETED:
                    # The existing activation-owned, shielded close task owns
                    # this work. No optional host write may revise the terminal
                    # Run or share its mutable snapshot with another consumer.
                    await self._turn_completed_consumer(_snapshot(snapshot))
        finally:
            self._closed.add(internal.run_id)
        # Shared Store/model lifecycle belongs to the injection owner.

    async def close_all(self) -> None:
        """Release every activation-owned handle before PluginHost disposal."""

        if self._shutdown is None:
            # Seal activation admission before yielding or closing transports.
            self._shutdown = asyncio.create_task(self._shutdown_activation())
            self._shutdown.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
        await asyncio.shield(self._shutdown)

    async def _shutdown_activation(self) -> None:
        admissions = tuple(self._admissions)
        for admission in admissions:
            admission.cancel()
        if admissions:
            await asyncio.gather(*admissions, return_exceptions=True)

        first_error: BaseException | None = None
        for internal in tuple(self._handles.values()):
            if internal.run_id in self._closed:
                continue
            try:
                await self.close(self._external(internal))
            except BaseException as error:  # all handles still need cleanup
                if first_error is None:
                    first_error = error
        callbacks, self._close_callbacks = self._close_callbacks, []
        for close in reversed(callbacks):
            try:
                await close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def capabilities(self) -> RuntimeCapabilityMatrix:
        available = RuntimeCapability(supported=True, mode="native")

        def missing(reason):
            return RuntimeCapability(supported=False, mode="unavailable", reason=reason)

        return RuntimeCapabilityMatrix(
            cancel=available,
            pause=available,
            resume=available,
            submit_interaction=available,
            attach=available,
            steer=available,
            guide=(available if self._engine._kernel.configuration.guidance_policy is not None
                   else missing("native_guidance_policy_unavailable")),
            child_cancellation=(available if self._engine._kernel.supports_child_cancellation()
                                else missing("native_child_cancellation_unavailable")),
            child_promotion=(
                available if self._engine._kernel.configuration.child_promotion_policy
                else missing("native_child_promotion_policy_unavailable")
            ),
            child_input=(available if self._engine._kernel.configuration.child_input_policy
                         else missing("native_child_input_policy_unavailable")),
            inject=missing("native_inject_unavailable"),
            checkpoint=missing("native_checkpoint_time_travel_unavailable"),
            durable_restore=(
                available if self._durable else missing("native_durable_store_unavailable")
            ),
            interaction_mode="durable_resume" if self._durable else "live_submit",
        )
