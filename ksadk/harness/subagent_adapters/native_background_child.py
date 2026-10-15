"""Single-process child owner, sharing the existing Native child execution path.

Handles own asyncio resources only. Run state, initial identity, reservations and
results remain in the original scoped Store and parent effect receipts. Missing
owners are never reconstructed by blindly launching the child again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, model_validator

from ksadk.harness.native.child_lifecycle import ChildLifecycleRequest, ChildLifecycleResult
from ksadk.harness.native.child_lifecycle_contract import (
    ChildInterruptedReceipt,
    ChildInterruptedReceiptRequest,
    ChildReadyRequest,
    ChildReadyResult,
    ChildResourceReleaseRequest,
)
from ksadk.harness.native.effect_admission import owned_child_admission
from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.model import ModelToolCall
from ksadk.harness.native.ports import (
    ChildBudgetSettlement,
    RunConfiguration,
    ToolDefinition,
    ToolRequest,
    ToolResult,
    digest,
)
from ksadk.harness.native.state import (
    ChildApproval,
    ChildBudgetReservation,
    Contract,
    RunScope,
    RunSnapshot,
)
from ksadk.harness.native.subagents import ChildRole, ChildRunRequest
from ksadk.harness.stores.native_contract import RunStore, ScopeMismatch, StoreConflict

from .native_child import ChildFactory, NativeChildToolPort, _settle
from .native_child_approval import (
    answer_original_pause,
    await_decision,
    current_approval,
    observe_until_ready,
)
from .native_child_continuation import bind_child_continuation
from .native_child_result import NativeChildResultPort


async def _join(tasks):
    return await asyncio.gather(*tasks, return_exceptions=True)


def _finish_startup(task: asyncio.Task, ready: asyncio.Future) -> None:
    # A Task cancelled before its first step never enters _drive's finally.
    # The owner still owes every launch waiter a settled readiness signal.
    if not ready.done():
        ready.set_result(False)
    if not task.cancelled():
        task.exception()


class _Launch(Contract):
    role: str = Field(min_length=1)
    task: str = Field(min_length=1, max_length=32000, pattern=r"\S")
    model_call_limit: int = Field(default=4, strict=True, ge=1)
    tool_call_limit: int = Field(default=8, strict=True, ge=0)
    output_format: Literal["text", "json_object"] | None = None
    output_contract: Literal["parent"] | None = None
    continuation_of: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"\S")
    background: bool = Field(default=True, strict=True)


class _Control(Contract):
    child_run_id: str = Field(min_length=1, max_length=256)
    action: Literal["wait", "send", "stop"]
    message: str | None = Field(default=None, min_length=1, max_length=32000, pattern=r"\S")
    wait_seconds: float = Field(default=15, ge=0, le=30, allow_inf_nan=False)

    @model_validator(mode="after")
    def message_only_for_send(self):
        if (self.action == "send") != (self.message is not None):
            raise ValueError("send requires a message; other actions cannot send one")
        return self


@dataclass
class _Handle:
    request: ChildRunRequest
    ready: asyncio.Future
    task: asyncio.Task | None = None
    kernel: NativeKernel | None = None
    reservation: ChildBudgetReservation | None = None
    background: bool = True
    promotion: asyncio.Event | None = None
    # Resource ownership is common; the original foreground receipt protocol
    # and its declared profile stay sealed until an explicit migration.
    sealed_runner: NativeChildToolPort | None = None
    approval: ChildApproval | None = None
    approval_ready: asyncio.Event = field(default_factory=asyncio.Event)
    approval_decision: asyncio.Future | None = None


class _GuidedChildToolPort(NativeChildToolPort):
    @staticmethod
    def _configuration(child, bindings):
        # Background send is supplemental guidance, not a replacement task.
        # Pin that supported control policy before the child Run is published;
        # synchronous delegates keep their original host configuration.
        config = NativeChildToolPort._configuration(child, bindings).model_dump(mode="json")
        config["guidance_policy"] = "boundary-guide/v1"
        return RunConfiguration.model_validate(config)


class NativeBackgroundChildToolPort:
    def __init__(
        self,
        *,
        scope: RunScope,
        roles: tuple[ChildRole, ...],
        factory: ChildFactory,
        store: RunStore,
        approval_bridge: bool = False,
    ):
        self._delegate = _GuidedChildToolPort(scope=scope, roles=roles, factory=factory)
        self.scope, self.store = self._delegate.scope, store
        self._reader = NativeChildResultPort(scope=scope, roles=roles, store=store)
        self._handles: dict[str, _Handle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self.approval_bridge = approval_bridge

    def definitions(self) -> tuple[ToolDefinition, ...]:
        properties = dict(self._delegate.definition().parameters["properties"])
        properties.update(
            background={"type": "boolean", "default": True, "description": (
                "False waits in the foreground. The host may move this same child to "
                "the background while it runs; true starts in the background immediately."
            )},
            model_call_limit={"type": "integer", "minimum": 1, "default": 4},
            tool_call_limit={"type": "integer", "minimum": 0, "default": 8},
            continuation_of={
                "type": "string", "minLength": 1, "maxLength": 256,
                "description": (
                    "Continue an already collected terminal child in its original session. "
                    "Use the same role and a new task; the returned Run ID is new. "
                    "First wait/collect the source result. Omit for an independent child."
                ),
            },
        )
        return (
            ToolDefinition(
                name="native_start_child",
                description=(
                    "Start a bounded approved child (background by default), then continue "
                    "independent "
                    "parent work. The startup result reserves shared budget; it is not findings. "
                    "Use native_child_control to send input, stop, wait and consume its result "
                    "before final delivery. No parent acceptance is inherited. For a follow-up, "
                    "provide continuation_of after collecting the previous result; its history "
                    "is retained but its immutable Run and usage are not reopened."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["role", "task"],
                    "properties": properties,
                },
                child_budget_policy="start/v1",
            ),
            ToolDefinition(
                name="native_child_control",
                description=(
                    "Control a child started by this parent: send additional input, stop it, "
                    "or wait up to 30 seconds and consume its terminal findings. A running "
                    "response is not completion; terminal results settle actual usage once. "
                    "A terminal child is immutable; use native_start_child with continuation_of "
                    "for a follow-up after collecting its result."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["child_run_id", "action"],
                    "properties": {
                        "child_run_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "action": {"type": "string", "enum": ["wait", "send", "stop"]},
                        "message": {"type": "string", "minLength": 1, "maxLength": 32000},
                        "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30},
                    },
                },
                child_budget_policy="settle/v1",
                child_approval_policy="owned-child-approval/v1" if self.approval_bridge else None,
            ),
        )

    def _child_request(self, request: ToolRequest) -> ChildRunRequest:
        launch = _Launch.model_validate(request.call.arguments)
        allowance = request.budget
        model_slots = allowance.max_model_calls_remaining - (
            0 if allowance.parent_finalization_reserved else 1
        )
        # Preserve a parent collection/control call rather than allocate every
        # remaining tool slot to the child. The outer final model reserve stays.
        tool_slots = allowance.max_tool_calls_remaining - 1
        if model_slots < 1 or tool_slots < 0:
            raise ValueError("child_budget_exhausted")
        args = {"role": launch.role, "task": launch.task}
        if launch.output_format is not None:
            args["output_format"] = launch.output_format
        if launch.output_contract is not None:
            args["output_contract"] = launch.output_contract
        adapted = request.model_copy(
            update={
                "call": ModelToolCall(
                    call_id=request.call.call_id, name="native_delegate", arguments=args
                ),
                "definition": self._delegate.definition(),
                "budget": allowance.model_copy(
                    update={
                        "max_model_calls_remaining": min(model_slots, launch.model_call_limit),
                        "max_tool_calls_remaining": min(tool_slots, launch.tool_call_limit),
                        "parent_finalization_reserved": True,
                    }
                ),
            }
        )
        child = self._delegate._request(adapted)
        if launch.continuation_of is None:
            return child
        child = bind_child_continuation(
            child, launch.continuation_of, store=self.store,
            roles=tuple(self._delegate.roles.values()),
        )
        self._check_continuation_resource(child)
        return child

    def _check_continuation_resource(self, child: ChildRunRequest) -> None:
        handle = self._handles.get(child.continuation_of)
        if handle is None:
            return
        if handle.request.role != child.role:
            raise ValueError("child_followup_role_mismatch")
        if handle.task is None or not handle.task.done():
            raise ValueError("child_followup_requires_result_collection")

    async def _drive(self, handle: _Handle) -> ToolResult:
        child = handle.request
        runner = handle.sealed_runner or self._delegate

        def ready(kernel: NativeKernel, config: RunConfiguration, snapshot: RunSnapshot):
            handle.kernel = kernel
            handle.reservation = ChildBudgetReservation(
                child_run_id=child.child_run_id,
                child_scope=child.scope,
                parent_operation_key=child.parent_operation_key,
                parent_tool_call_id=child.parent_tool_call_id,
                child_request_digest=digest(child),
                configuration_digest=config.digest,
                max_model_calls=config.limits.max_model_calls,
                max_tool_calls=config.limits.max_tool_calls,
            )
            handle.ready.set_result(True)

        try:
            async def interaction(kernel, snapshot):
                await await_decision(handle, kernel, snapshot)

            result = await runner._run(
                child, digest(child), on_ready=ready,
                on_interaction=interaction if self.approval_bridge else None,
            )
            runner._results[child.child_run_id] = (digest(child), result)
            return result
        finally:
            if not handle.ready.done():
                handle.ready.set_result(False)

    async def execute_foreground(
        self, request: ToolRequest, *, delegate: NativeChildToolPort
    ) -> ToolResult:
        """Own the declared delegate, preserving legacy sealed SDK delivery."""
        try:
            request = ToolRequest.model_validate_json(request.model_dump_json())
            if delegate.scope != self.scope or delegate.roles != self._delegate.roles:
                raise ValueError("child_scope_or_definition_mismatch")
            child = delegate._request(request)
            self._check_continuation_resource(child)
        except (ValueError, TypeError) as error:
            codes = {"child_scope_or_definition_mismatch", "child_arguments_invalid",
                     "child_budget_exhausted", "child_followup_not_available",
                     "child_followup_role_mismatch", "child_followup_requires_result_collection"}
            return delegate._error(str(error) if str(error) in codes else "child_request_rejected")
        async with self._locks.setdefault(child.child_run_id, asyncio.Lock()):
            if self._closed:
                return delegate._error("child_owner_closed")
            handle = self._handles.get(child.child_run_id)
            if handle is not None and (
                handle.request != child or handle.sealed_runner is not delegate
            ):
                return delegate._error("child_operation_conflict")
            # The original runner seals a cancellation receipt before its
            # Task raises. Repeated callers consume that receipt, not the
            # cancelled Task, and must never relaunch its effects.
            cached = delegate._results.get(child.child_run_id)
            if cached is not None:
                if cached[0] != digest(child):
                    return delegate._error("child_operation_conflict")
                if (child.delivery_mode is None or child.delivery_mode == "foreground"
                        and not handle.promotion.is_set()):
                    return ToolResult.model_validate_json(cached[1].model_dump_json())
                return self._startup_result(handle)
            if handle is None:
                with owned_child_admission():
                    handle = _Handle(child, asyncio.get_running_loop().create_future(),
                                     background=child.delivery_mode == "background",
                                     promotion=asyncio.Event() if child.delivery_mode else None,
                                     sealed_runner=delegate)
                    self._handles[child.child_run_id] = handle
                    handle.task = asyncio.create_task(self._drive(handle))
                handle.task.add_done_callback(
                    lambda completed: _finish_startup(completed, handle.ready)
                )
            if child.delivery_mode is not None:
                return await self._deliver_owned(handle, sealed_foreground=True)
            try:
                result = await asyncio.shield(handle.task)
            except asyncio.CancelledError:
                handle.task.cancel()
                await _settle(_join((handle.task,)), propagate_cancel=False)
                raise
            return ToolResult.model_validate_json(result.model_dump_json())

    @staticmethod
    def _startup_result(handle: _Handle) -> ToolResult:
        return ToolResult(
            status="ok",
            content={
                "child_run_id": handle.request.child_run_id,
                "started": True,
                "parent_acceptance": "not_evaluated",
                "result_consumed": False,
                "control_tool": "native_child_control",
                "background": handle.background or handle.promotion.is_set(),
            },
            child_budget_reservation=handle.reservation,
        )

    async def _deliver_owned(self, handle: _Handle, *, sealed_foreground: bool) -> ToolResult:
        try:
            ready = await asyncio.shield(handle.ready)
            if not ready:
                return await handle.task
            if not handle.background:
                promotion = asyncio.create_task(handle.promotion.wait())
                approval = asyncio.create_task(handle.approval_ready.wait())
                try:
                    await asyncio.wait((handle.task, promotion, approval),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    promotion.cancel()
                    approval.cancel()
                    await _settle(_join((promotion, approval)), propagate_cancel=False)
                if handle.task.done() and (
                    not sealed_foreground or not handle.promotion.is_set()
                ):
                    result = await handle.task
                    if sealed_foreground:
                        return ToolResult.model_validate_json(result.model_dump_json())
            return self._startup_result(handle)
        except asyncio.CancelledError:
            handle.task.cancel()
            await _settle(_join((handle.task,)), propagate_cancel=False)
            raise

    async def _start(self, request: ToolRequest) -> ToolResult:
        launch = _Launch.model_validate(request.call.arguments)
        try:
            child = self._child_request(request)
        except ValueError as error:
            if str(error) not in {
                "child_followup_not_available", "child_followup_role_mismatch",
                "child_followup_requires_result_collection", "child_budget_exhausted",
            }:
                raise
            return self._delegate._error(str(error))
        async with self._locks.setdefault(child.child_run_id, asyncio.Lock()):
            if self._closed:
                return self._delegate._error("child_owner_closed")
            handle = self._handles.get(child.child_run_id)
            if handle is not None and (
                handle.request != child or handle.background != launch.background
            ):
                return self._delegate._error("child_operation_conflict")
            if handle is None:
                with owned_child_admission():
                    handle = _Handle(child, asyncio.get_running_loop().create_future())
                    handle.background = launch.background
                    handle.promotion = asyncio.Event()
                    self._handles[child.child_run_id] = handle
                    handle.task = asyncio.create_task(self._drive(handle))
                handle.task.add_done_callback(
                    lambda completed: _finish_startup(completed, handle.ready)
                )
            return await self._deliver_owned(handle, sealed_foreground=False)

    def _waiting_handle(self, parent: RunSnapshot, child_run_id: str, *, read_closed=False):
        handle = self._handles.get(child_run_id)
        if (self._closed and not read_closed or parent.scope != self.scope or parent.phase.terminal
                or handle is None or handle.kernel is None or handle.reservation is None
                or handle.request.parent_scope != parent.scope
                or handle.request.parent_run_id != parent.run_id or parent.pending_intent is None):
            return None
        intent = self.store.get_intent(self.scope, parent.run_id, parent.pending_intent)
        call = None
        if intent is not None and handle.request.parent_operation_key == intent.operation_key:
            call = intent.payload.get("call")
        elif intent is not None and intent.payload.get("parallel_delegation") is True:
            for index, member in enumerate(intent.payload.get("calls", ()), 1):
                if handle.request.parent_operation_key == f"{intent.operation_key}:call:{index}":
                    call = member
                    break
        name = "native_delegate" if handle.sealed_runner is not None else "native_start_child"
        if (intent is None or intent.kind != "tool" or intent.status != "running"
                or not isinstance(call, dict) or call.get("name") != name
                or call.get("call_id") != handle.request.parent_tool_call_id):
            return None
        args = call.get("arguments")
        child = self.store.load(handle.request.scope, child_run_id)
        if (not isinstance(args, dict) or args.get("role") != handle.request.role.role_id
                or args.get("task") != handle.request.task
                or child.origin is None or child.origin.get("parent_run_id") != parent.run_id
                or child.origin.get("parent_scope") != parent.scope.model_dump(mode="json")
                or child.origin.get("parent_operation_key") != handle.request.parent_operation_key
                or child.origin.get("parent_tool_call_id") != handle.request.parent_tool_call_id
                or child.configuration_digest != handle.reservation.configuration_digest):
            return None
        return handle

    def _foreground_handle(self, parent: RunSnapshot, child_run_id: str):
        handle = self._waiting_handle(parent, child_run_id)
        if (handle is None or (handle.sealed_runner is not None
                and handle.request.delivery_mode is None) or handle.background
                or handle.task.done()
                or self.store.load(handle.request.scope, child_run_id).phase.terminal):
            return None
        return handle

    def can_promote(self, parent: RunSnapshot, child_run_id: str) -> bool:
        handle = self._foreground_handle(parent, child_run_id)
        return handle is not None and not handle.promotion.is_set()

    def release_foreground(self, parent: RunSnapshot, child_run_id: str) -> None:
        # The Kernel commits child.promoted before releasing this local wait.
        # Both operations are synchronous under its original driver ownership.
        handle = self._foreground_handle(parent, child_run_id)
        if handle is not None:
            handle.promotion.set()

    def child_tasks(self, parent: RunSnapshot) -> tuple[dict, ...]:
        if parent.scope != self.scope:
            raise ValueError("child_task_scope_mismatch")
        rows = []
        promoted_ids = {e.payload.get("child_run_id")
                        for e in self.store.events(self.scope, parent.run_id)
                        if e.kind == "child.promoted"}
        for child_id in dict.fromkeys((*parent.child_run_ids, *self._handles)):
            handle = self._waiting_handle(parent, child_id)
            child = (self.store.load(handle.request.scope, child_id) if handle is not None
                     else self._reader._load_direct_child(parent, child_id))
            if child is None:
                continue
            original = self.store.get_intent(
                self.scope, parent.run_id, child.origin["parent_operation_key"]
            )
            original_call = original.payload.get("call", {}) if original is not None else {}
            if handle is not None:
                foreground = not handle.background and child_id not in promoted_ids
            else:
                foreground = (
                    original_call.get("name") == "native_delegate"
                    and original_call.get("arguments", {}).get("background", False) is False
                    or original_call.get("name") == "native_start_child"
                    and original_call.get("arguments", {}).get("background") is False
                ) and child_id not in promoted_ids
            rows.append({"child_run_id": child_id, "session_id": child.session_id,
                         "role": child.origin.get("role"), "phase": child.phase.value,
                         "delivery": "foreground" if foreground else "background",
                         "can_promote": self.can_promote(parent, child_id),
                         "can_cancel": self.can_cancel(parent, child_id),
                         "goal_revision": child.goal_revision,
                         "can_send_input": self.child_input_target(parent, child_id) is not None})
        return tuple(rows)

    def can_cancel(self, parent: RunSnapshot, child_run_id: str) -> bool:
        handle = self._waiting_handle(parent, child_run_id)
        child = (self.store.load(handle.request.scope, child_run_id) if handle is not None
                 else self._reader._load_direct_child(parent, child_run_id))
        handle = handle or self._handles.get(child_run_id)
        return bool(not self._closed and parent.scope == self.scope and not parent.phase.terminal
                    and child is not None and not child.phase.terminal and handle is not None
                    and handle.kernel is not None and not handle.task.done()
                    and handle.request.parent_run_id == parent.run_id
                    and handle.request.parent_scope == parent.scope
                    and handle.request.scope == child.scope)

    async def cancel_child(self, parent: RunSnapshot, child_run_id: str) -> None:
        if not self.can_cancel(parent, child_run_id):
            return  # The child may have naturally finished after admission.
        task = self._handles[child_run_id].task
        if not task.cancelling():
            task.cancel()
        # Join the existing runner's bounded cleanup; it remains the authority
        # for child terminal facts and usage. A slow cleanup is still not success.
        await asyncio.wait((task,), timeout=1.0)

    def child_execution_view(self, parent: RunSnapshot, child_run_id: str) -> dict:
        """Read only an admitted direct child through its original scope/receipt."""
        from ksadk.harness.native_projection import NativeEventProjection

        if parent.scope != self.scope:
            raise ValueError("child_task_scope_mismatch")
        handle = self._waiting_handle(parent, child_run_id)
        child = (self.store.load(handle.request.scope, child_run_id) if handle is not None
                 else self._reader._load_direct_child(parent, child_run_id))
        if child is None:
            raise ValueError("child_not_owned")
        projection = NativeEventProjection(child.scope, child.run_id)
        events = [event.model_dump(mode="json")
                  for native in self.store.events(child.scope, child.run_id)
                  for event in projection.consume(native)]
        return {"child_run_id": child.run_id, "session_id": child.session_id,
                "call_id": child.origin.get("parent_tool_call_id"),
                "role": child.origin.get("role"), "phase": child.phase.value,
                "events": events}

    def child_input_target(self, parent: RunSnapshot, child_run_id: str):
        from ksadk.harness.native.child_input import ChildInputTarget

        handle = self._waiting_handle(parent, child_run_id)
        child = (self.store.load(handle.request.scope, child_run_id) if handle is not None
                 else self._reader._load_direct_child(parent, child_run_id))
        handle = handle or self._handles.get(child_run_id)
        if (self._closed or parent.scope != self.scope or parent.phase.terminal
                or child is None or child.phase.terminal or handle is None
                or handle.kernel is None or handle.task.done()
                or handle.kernel.configuration.guidance_policy is None
                or handle.request.parent_run_id != parent.run_id
                or handle.request.parent_scope != parent.scope
                or handle.request.scope != child.scope):
            return None
        return ChildInputTarget(scope=child.scope, run_id=child.run_id,
                                configuration_digest=child.configuration_digest,
                                parent_operation_key=handle.request.parent_operation_key,
                                parent_tool_call_id=handle.request.parent_tool_call_id)

    def wake_child_input(self, parent: RunSnapshot, child_run_id: str) -> None:
        target = self.child_input_target(parent, child_run_id)
        if target is not None:
            self._handles[child_run_id].kernel.notify_pending_commands(child_run_id)

    def interrupted_receipts(
        self, request: ChildInterruptedReceiptRequest
    ) -> tuple[ChildInterruptedReceipt, ...]:
        request = ChildInterruptedReceiptRequest.model_validate_json(request.model_dump_json())
        if request.scope != self.scope:
            raise ValueError("child_interrupted_receipt_scope_mismatch")
        parent = self.store.load(self.scope, request.run_id)
        if parent.pending_intent != request.parent_operation_key or parent.phase.terminal:
            raise ValueError("child_interrupted_receipt_parent_changed")
        results = []
        for child_id in self._handles:
            handle = self._waiting_handle(parent, child_id, read_closed=True)
            if handle is None or (handle.sealed_runner is not None
                                  and handle.request.delivery_mode is None):
                continue
            # Reading a known receipt never runs a factory or model, and does
            # not require a cancelled Task to return successfully. In-progress
            # owned resources enter the parent's existing cancellation ledger.
            runner = handle.sealed_runner or self._delegate
            cached = runner._results.get(child_id)
            child = self.store.load(handle.request.scope, child_id)
            if (handle.request.delivery_mode == "foreground"
                    and handle.task.done() and child.phase.terminal and cached is not None
                    and cached[0] == digest(handle.request)
                    and not handle.background and not handle.promotion.is_set()):
                result = ToolResult.model_validate_json(cached[1].model_dump_json())
            else:
                result = self._startup_result(handle)
            results.append(ChildInterruptedReceipt(
                parent_operation_key=handle.request.parent_operation_key,
                parent_tool_call_id=handle.request.parent_tool_call_id,
                result=result,
            ))
        return tuple(results)

    async def _control(self, request: ToolRequest) -> ToolResult:
        control = _Control.model_validate(request.call.arguments)
        parent = self.store.load(self.scope, request.run_id)
        child = self._reader._load_direct_child(parent, control.child_run_id)
        handle = self._handles.get(control.child_run_id)
        if child is None or handle is None or handle.kernel is None:
            return self._delegate._error("child_owner_unavailable_or_not_authorized")
        if not any(item.child_run_id == child.run_id for item in parent.child_budget_reservations):
            # Its original receipt already charged usage. Returning an
            # allocation settlement would charge it again or break the parent
            # reducer. Keep reads on the existing scoped result query seam.
            return ToolResult(status="error", content={
                "error_code": "child_already_accounted_use_result_tool",
                "result_tool": "native_child_result",
            })
        if request.child_approval is not None:
            if request.child_approval.child_run_id != child.run_id or control.action != "wait":
                raise ValueError("child_approval_control_changed")
            answer_original_pause(self, parent, request)
        if not child.phase.terminal and control.action == "send":
            accepted = handle.kernel.guide(
                child.run_id,
                text=control.message,
                command_id=request.operation_key,
                expected_goal_revision=handle.kernel.reserved_goal_revision(child.run_id),
            )
            return ToolResult(
                status="ok",
                content={
                    "child_run_id": child.run_id,
                    "input_status": accepted.status,
                    "input_reason": accepted.reason,
                    "result_consumed": False,
                },
            )
        if control.action == "send":
            return self._delegate._error("child_terminal_requires_new_run")
        if control.action == "stop" and not handle.task.done():
            handle.task.cancel()
            await _settle(_join((handle.task,)))
        elif not handle.task.done() and control.wait_seconds:
            # Timeout ends this observation only; shield the independently
            # owned child so a bounded poll cannot cancel its work.
            try:
                await asyncio.wait_for(observe_until_ready((handle,)), control.wait_seconds)
            except TimeoutError:
                pass
        if not handle.task.done():
            if (approval := current_approval(handle)) is not None:
                from ksadk.harness.native.child_approval import host_id

                return ToolResult(status="approval_required", interaction_id=host_id(approval),
                                  child_approval=approval,
                                  content={"child_run_id": child.run_id})
            current = self.store.load(child.scope, child.run_id)
            return ToolResult(
                status="ok",
                content={
                    "child_run_id": child.run_id,
                    "phase": current.phase.value,
                    "result_consumed": False,
                    "parent_acceptance": "not_evaluated",
                },
            )
        return self._terminal_result(handle)

    def _terminal_result(self, handle: _Handle) -> ToolResult:
        child = handle.request
        runner = handle.sealed_runner or self._delegate
        cached = runner._results.get(child.child_run_id)
        if cached is None:
            return self._delegate._error("child_terminal_receipt_unavailable")
        current = self.store.load(child.scope, child.child_run_id)
        if not current.phase.terminal:
            return ToolResult(status="uncertain", content={"error_code": "child_owner_lost"})
        return cached[1].model_copy(
            deep=True,
            update={
                "child_budget_settlement": ChildBudgetSettlement(
                    child_run_id=child.child_run_id,
                    parent_operation_key=handle.request.parent_operation_key,
                    child_request_digest=digest(handle.request),
                    terminal_snapshot_digest=digest(current),
                ),
            },
        )

    async def collect(self, request: ChildLifecycleRequest) -> ChildLifecycleResult:
        request = ChildLifecycleRequest.model_validate_json(request.model_dump_json())
        if request.scope != self.scope:
            raise ValueError("child_lifecycle_scope_mismatch")
        parent = self.store.load(self.scope, request.run_id)
        expected = tuple(item for item in parent.child_budget_reservations
                         if item.settled_usage_digest is None)
        if (parent.phase.terminal or parent.pending_intent != request.operation_key
                or expected != request.reservations):
            raise ValueError("child_lifecycle_parent_changed")
        if request.child_approval is not None:
            answer_original_pause(self, parent, request)
        handles, pending, error = [], [], None
        for allocation in request.reservations:
            child = self._reader._load_direct_child(parent, allocation.child_run_id)
            handle = self._handles.get(allocation.child_run_id)
            if (child is None or handle is None or handle.kernel is None
                    or handle.reservation != allocation):
                pending.append(allocation.child_run_id)
                error = "child_owner_unavailable"
            else:
                handles.append(handle)
        if request.action == "cancel":
            for handle in handles:
                if not handle.task.done():
                    handle.task.cancel()
            await _settle(_join(tuple(handle.task for handle in handles)))
        elif request.action == "wait":
            # The enclosing parent effect owns a deadline and cancellation.
            # Ending observation cannot itself cancel an independent child.
            if self.approval_bridge:
                await observe_until_ready(handles)
            else:
                await asyncio.gather(*(asyncio.shield(handle.task) for handle in handles),
                                     return_exceptions=True)
        results, approvals = [], []
        for handle in handles:
            if not handle.task.done():
                pending.append(handle.request.child_run_id)
                if self.approval_bridge and (approval := current_approval(handle)) is not None:
                    approvals.append(approval)
                continue
            result = self._terminal_result(handle)
            if result.child_budget_settlement is None:
                pending.append(handle.request.child_run_id)
                error = "child_receipt_unavailable"
            else:
                results.append(result)
        return ChildLifecycleResult(
            operation_key=request.operation_key, results=tuple(results),
            pending_child_run_ids=tuple(pending), error_code=error,
            approvals=tuple(approvals),
        )

    def ready_children(self, request: ChildReadyRequest) -> ChildReadyResult:
        """Hint from original owned terminal receipts; never mutate parent facts."""
        request = ChildReadyRequest.model_validate_json(request.model_dump_json())
        if request.scope != self.scope:
            raise ValueError("child_lifecycle_scope_mismatch")
        parent = self.store.load(self.scope, request.run_id)
        expected = tuple(item for item in parent.child_budget_reservations
                         if item.settled_usage_digest is None)
        if parent.phase.terminal or expected != request.reservations:
            raise ValueError("child_lifecycle_parent_changed")
        ready = []
        for allocation in request.reservations:
            child = self._reader._load_direct_child(parent, allocation.child_run_id)
            handle = self._handles.get(allocation.child_run_id)
            if (child is not None and handle is not None and handle.reservation == allocation
                    and handle.task is not None and (
                        handle.task.done()
                        and self._terminal_result(handle).child_budget_settlement is not None
                        or request.include_approvals and self.approval_bridge
                        and current_approval(handle) is not None
                    )):
                ready.append(allocation.child_run_id)
        return ChildReadyResult(child_run_ids=tuple(ready))

    async def execute(self, request: ToolRequest) -> ToolResult:
        request = ToolRequest.model_validate_json(request.model_dump_json())
        definitions = {item.name: item for item in self.definitions()}
        if self._closed:
            return self._delegate._error("child_owner_closed")
        if request.scope != self.scope or request.definition != definitions.get(request.call.name):
            return self._delegate._error("child_scope_or_definition_mismatch")
        try:
            if request.call.name == "native_start_child":
                return await self._start(request)
            return await self._control(request)
        except (ValueError, KeyError, ScopeMismatch, StoreConflict):
            return self._delegate._error("child_lifecycle_request_rejected")

    async def close(self):
        self._closed = True
        tasks = [handle.task for handle in self._handles.values() if handle.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await _settle(_join(tasks))

    async def release_run(self, request: ChildResourceReleaseRequest) -> None:
        request = ChildResourceReleaseRequest.model_validate_json(request.model_dump_json())
        if request.scope != self.scope:
            raise ValueError("child_release_scope_mismatch")
        tasks = [handle.task for handle in self._handles.values()
                 if handle.request.parent_run_id == request.run_id
                 and handle.request.parent_scope == request.scope and handle.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await _settle(_join(tasks))
        # The activation is reusable by later parent turns. Do not close the
        # owner, mutate parent facts, or stop another parent's resources.
