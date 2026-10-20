"""Synchronous delegated ToolPort, reusing the NativeKernel for every child.

The host factory owns all credentials, policies, workspace isolation and cleanup.
This adapter does not provide a distributed scheduler, mailbox or approval bridge.
It never writes the parent store; parent accounting consumes its sealed receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from ksadk.harness.artifact_adapters.native_page_budget import ArtifactPagePolicy
from ksadk.harness.artifact_adapters.native_search_budget import ArtifactSearchPolicy
from ksadk.harness.artifact_adapters.native_tool import install_artifact_tool
from ksadk.harness.native.call_budget import call_room, cap_calls, remaining_calls
from ksadk.harness.stores.native_contract import RunStore, StoreConflict

from ..native.completion import TaskContract
from ..native.context_contributions import ContextPort
from ..native.context_runtime import ContextPlanner
from ..native.engine import NativeKernel
from ..native.hook_runtime_contract import HookPort
from ..native.output_contract import includes_object_constraint, object_output_schema
from ..native.ports import (
    ArtifactPort,
    ModelPort,
    RunConfiguration,
    ToolDefinition,
    ToolPort,
    ToolRequest,
    ToolResourceUsage,
    ToolResult,
    VerificationPort,
    WorkspacePort,
    digest,
)
from ..native.state import RunCommand, RunPhase, RunScope, RunSnapshot
from ..native.subagents import ChildMediaRef, ChildObservation, ChildRole, ChildRunRequest
from ..native.user_input import NativeUserInput


@dataclass(frozen=True)
class ChildBindings:
    """Trusted role assembly. A policy-bound workspace is an explicit host assertion.

    Tools must enforce the declared access mode. Write roles require an isolated
    worktree/sandbox identity; an arbitrary shared working directory is rejected.
    Cleanup and all effect ports must cooperate with cancellation.
    """

    store: RunStore
    configuration: RunConfiguration
    model: ModelPort
    workspace_revision: str
    tools: ToolPort | None = None
    verifier: VerificationPort | None = None
    workspace: WorkspacePort | None = None
    context_planner: ContextPlanner | None = None
    workspace_access: Literal["read_only", "isolated_write"] = "read_only"
    workspace_policy_bound: bool = False
    isolation_id: str | None = None
    cleanup: Callable[[], Awaitable[None]] | None = None
    # Durable hosts bind independent child ownership. None preserves the
    # original fencing-only SDK mode; a parent lease cannot protect a child.
    lease_ttl_seconds: float | None = None
    # Only explicit parent-output-reference callers consume this host binding.
    parent_contract: TaskContract | None = None
    # Host-created for the child's authenticated scope. Only refs earned by
    # its own observations are admitted; no parent path or ref inheritance.
    artifacts: ArtifactPort | None = None
    artifact_search: bool = False
    artifact_catalog: bool = False
    artifact_page_policy: ArtifactPagePolicy | None = None
    artifact_search_policy: ArtifactSearchPolicy | None = None
    # Child-owned host bindings, consumed only by its declared configuration.
    # Parent ports and their authority are never inherited by this adapter.
    context_port: ContextPort | None = None
    hook_port: HookPort | None = None
    # Host-resolved user input for the child run. Built from authorized parent
    # media references; None retains the original plain-text goal identity.
    user_input: NativeUserInput | None = None


ChildFactory = Callable[[ChildRunRequest], ChildBindings | Awaitable[ChildBindings]]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _settle(operation, *, propagate_cancel: bool = True):
    """Wait for cleanup even when the caller repeats cancellation."""
    task = asyncio.create_task(operation)
    canceled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            canceled = True
    result = task.result()
    if canceled and propagate_cancel:
        raise asyncio.CancelledError
    return result


class NativeChildToolPort:
    def __init__(self, *, scope: RunScope, roles: tuple[ChildRole, ...], factory: ChildFactory):
        self.scope = RunScope.model_validate_json(scope.model_dump_json())
        self.roles = {
            role.role_id: ChildRole.model_validate_json(role.model_dump_json()) for role in roles
        }
        if not roles or len(self.roles) != len(roles) or not callable(factory):
            raise ValueError("unique host roles and a factory are required")
        self.factory = factory
        self._locks: dict[str, asyncio.Lock] = {}
        self._results: dict[str, tuple[str, ToolResult]] = {}

    def definition(self) -> ToolDefinition:
        parallel = all(
            role.concurrent_safe and role.workspace_access == "read_only"
            for role in self.roles.values()
        )
        return ToolDefinition(
            name="native_delegate",
            description=(
                "Run one bounded child task using an approved role; "
                "its result is evidence, not parent acceptance."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["role", "task"],
                "properties": {
                    "role": {"type": "string", "enum": list(self.roles)},
                    "task": {"type": "string", "minLength": 1, "maxLength": 32000},
                    "output_format": {
                        "type": "string",
                        "enum": ["text", "json_object"],
                        "default": "text",
                        "description": (
                            "Use text for ordinary prose, a bare number, or a word. "
                            "Use json_object only when the child is requested to deliver "
                            "one JSON object. Bound role and task contracts still apply; "
                            "this is not semantic verification."
                        ),
                    },
                    "output_contract": {
                        "type": "string",
                        "enum": ["parent"],
                        "description": (
                            "Select parent when delegating delivery in the parent's current "
                            "requested object format. Requires output_format=json_object. "
                            "The host preserves its exact schema and locked role constraints; "
                            "acceptance, tools and budget are not inherited. Omit for a "
                            "subtask with its own output format."
                        ),
                    },
                    "media": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["source", "index"],
                            "properties": {
                                "source": {
                                    "type": "string",
                                    "enum": ["parent_user_input"],
                                    "description": (
                                        "The only authorized media source: a part of this "
                                        "parent run's current user input."
                                    ),
                                },
                                "index": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "description": (
                                        "Position of the image part in the parent user "
                                        "input content. The host resolves it to the original "
                                        "image bytes; it never accepts a model-supplied path, "
                                        "URL or data URL."
                                    ),
                                },
                            },
                        },
                        "description": (
                            "Pass already-admitted parent user input image parts to the "
                            "child by scoped reference, not by value. The host fails closed on "
                            "unknown, forged or foreign references and on an image-incapable "
                            "child role. Omit for a text-only subtask."
                        ),
                    },
                },
            },
            read_only=parallel,
            concurrent_safe=parallel,
            delegated_read_only=parallel,
        )

    @staticmethod
    def _error(code: str) -> ToolResult:
        return ToolResult(status="error", content={"error_code": code})

    @staticmethod
    def _parse_media_refs(raw) -> tuple[ChildMediaRef, ...]:
        # Structural validation only. The host authorizes the source and the
        # addressable parent parts later, against the actual parent run. The
        # model can never place bytes, paths, URLs or data URLs here: extra
        # fields are rejected by the frozen contract before any store load.
        if raw is None:
            return ()
        if not isinstance(raw, list) or len(raw) > 20:
            raise ValueError("child_arguments_invalid")
        refs: list[ChildMediaRef] = []
        for item in raw:
            try:
                refs.append(ChildMediaRef.model_validate(item))
            except (ValueError, TypeError):
                raise ValueError("child_arguments_invalid")
        return tuple(refs)

    def _request(self, request: ToolRequest) -> ChildRunRequest:
        if (
            request.scope != self.scope
            or request.call.name != "native_delegate"
            or request.definition != self.definition()
        ):
            raise ValueError("child_scope_or_definition_mismatch")
        args = request.call.arguments
        dual_mode = request.definition.child_budget_policy == "delegate/v1"
        allowed = {"role", "task", "output_format", "output_contract", "media"}
        if dual_mode:
            allowed.add("background")
        if (
            not {"role", "task"}.issubset(args)
            or not set(args).issubset(allowed)
            or ("background" in args and type(args["background"]) is not bool)
            or not isinstance(args["role"], str)
            or args["role"] not in self.roles
            or ("output_format" in args and args["output_format"] not in ("text", "json_object"))
            or ("output_contract" in args and args["output_contract"] != "parent")
            or ("output_contract" in args and args.get("output_format") != "json_object")
        ):
            raise ValueError("child_arguments_invalid")
        if (
            not isinstance(args["task"], str)
            or not args["task"].strip()
            or len(args["task"]) > 32000
        ):
            raise ValueError("child_arguments_invalid")
        media_refs = self._parse_media_refs(args.get("media"))
        if (
            # The delegated result returns as a tool receipt. The parent must
            # retain one model turn to read it and produce or repair its answer.
            not call_room(request.budget.max_model_calls_remaining, 0,
                          1 if request.budget.parent_finalization_reserved else 2)
            or request.budget.depth >= request.budget.max_depth
        ):
            raise ValueError("child_budget_exhausted")
        role = self.roles[args["role"]]
        scope = request.scope.model_copy(
            update={"agent_id": role.agent_id or request.scope.agent_id}
        )
        key = _digest(
            {
                "scope": request.scope.model_dump(mode="json"),
                "parent_run_id": request.run_id,
                "operation_key": request.operation_key,
            }
        )
        return ChildRunRequest(
            scope=scope,
            parent_scope=request.scope,
            child_run_id="child-" + key,
            child_session_id="child-session-" + key,
            parent_run_id=request.run_id,
            parent_tool_call_id=request.call.call_id,
            parent_operation_key=request.operation_key,
            task=args["task"],
            delivery_mode=("background" if args.get("background", False) else "foreground")
            if dual_mode else None,
            output_format=args.get("output_format"),
            output_contract=args.get("output_contract"),
            media_refs=media_refs,
            role=role,
            budget=request.budget,
        )

    @staticmethod
    def _configuration(child: ChildRunRequest, bindings: ChildBindings) -> RunConfiguration:
        config = RunConfiguration.model_validate_json(bindings.configuration.model_dump_json())
        if child.output_format == "json_object" and (
            config.contract.final_submission_schema is None
            and (
                config.final_output_format != "json_object"
                or config.final_submission_schema is None
            )
        ):
            raise ValueError("child_output_contract_not_bound")
        if child.output_contract == "parent":
            parent = (
                TaskContract.model_validate_json(bindings.parent_contract.model_dump_json())
                if bindings.parent_contract is not None
                else None
            )
            source = (
                object_output_schema(
                    parent.final_submission_schema, parent.final_submission_payload_key
                )
                if parent is not None
                else None
            )
            if source is None:
                raise ValueError("child_parent_output_contract_unavailable")
            selected = config.contract if config.contract.final_submission_schema else config
            if not includes_object_constraint(
                selected.final_submission_schema, selected.final_submission_payload_key, source
            ):
                raise ValueError("child_parent_output_contract_not_bound")
        if not set(tool.name for tool in config.tools).issubset(child.role.tool_names):
            raise ValueError("child_capability_grant_mismatch")
        if (
            bindings.workspace_access != child.role.workspace_access
            or not bindings.workspace_revision.strip()
        ):
            raise ValueError("child_workspace_grant_mismatch")
        if config.tools and (bindings.tools is None or not bindings.workspace_policy_bound):
            raise ValueError("child_workspace_policy_required")
        if bindings.workspace_access == "isolated_write" and (
            not bindings.isolation_id or not bindings.workspace_policy_bound
        ):
            raise ValueError("child_workspace_isolation_required")
        if child.role.concurrent_safe and (
            bindings.workspace_access != "read_only"
            or any(not tool.read_only for tool in config.tools)
        ):
            raise ValueError("child_parallel_read_only_grant_invalid")
        raw = config.model_dump(mode="json")
        raw["limits"]["max_model_calls"] = cap_calls(
            config.limits.max_model_calls,
            remaining_calls(child.budget.max_model_calls_remaining, 0,
                            0 if child.budget.parent_finalization_reserved else 1),
        )
        raw["limits"]["max_tool_calls"] = cap_calls(
            config.limits.max_tool_calls, child.budget.max_tool_calls_remaining
        )
        raw["limits"]["max_subagent_depth"] = min(
            config.limits.max_subagent_depth, child.budget.max_depth
        )
        raw["delegation_depth"] = child.budget.depth + 1
        return RunConfiguration.model_validate(raw)

    @staticmethod
    async def _cancel(kernel: NativeKernel, child: ChildRunRequest) -> RunSnapshot:
        snapshot = kernel.snapshot(child.child_run_id)
        if not snapshot.phase.terminal:
            kernel.submit(
                child.child_run_id,
                RunCommand(command_id=child.child_run_id + ":parent-stop", kind="cancel"),
            )
            snapshot = await kernel.run_until_idle(child.child_run_id)
        return snapshot

    @staticmethod
    def _result(
        child: ChildRunRequest,
        kernel: NativeKernel,
        snapshot: RunSnapshot,
        *,
        code: str | None = None,
        uncertain: bool = False,
    ) -> ToolResult:
        events = kernel.events(snapshot.run_id)
        uncertain = uncertain or snapshot.phase == RunPhase.INTERRUPTED
        for event in events:
            if event.kind == "effect.prepared":
                receipt = kernel.store.get_receipt(
                    snapshot.scope, snapshot.run_id, event.payload["operation_key"]
                )
                if receipt is not None and (
                    receipt.status == "uncertain" or receipt.result.get("status") == "uncertain"
                ):
                    uncertain = True
        if uncertain and code is None:
            code = "child_effect_outcome_unknown"
        usage_events = [
            event
            for event in events
            if event.kind == "usage.reported" and event.payload.get("semantics") == "delta"
        ]
        direct_reports = [
            event for event in usage_events if event.payload.get("source") != "descendant_runs"
        ]
        known = (
            not uncertain
            and len(direct_reports) == snapshot.model_calls
            and not any(event.kind == "usage.unavailable" for event in events)
        )
        if snapshot.descendant_model_calls and not any(
            event.payload.get("source") == "descendant_runs" for event in usage_events
        ):
            known = False
        tokens = {}
        for name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        ):
            values = [event.payload.get(name) for event in usage_events]
            tokens[name] = (
                sum(values)
                if known and all(type(value) is int and value >= 0 for value in values)
                else None
            )
        report = snapshot.verification or {}
        observation = ChildObservation(
            child_run_id=snapshot.run_id,
            child_scope=snapshot.scope,
            parent_run_id=child.parent_run_id,
            parent_tool_call_id=child.parent_tool_call_id,
            phase=snapshot.phase.value,
            outcome=snapshot.outcome,
            error_code=code,
            final_text=snapshot.final_text[:16000],
            text_truncated=len(snapshot.final_text) > 16000,
            evidence_refs=tuple(
                ref for item in report.get("items", ()) for ref in item.get("evidence_refs", ())
            ),
            resource_usage_unknown=uncertain,
        )
        return ToolResult(
            status="uncertain" if uncertain else "error" if code else "ok",
            content=observation.model_dump(mode="json"),
            resource_usage=ToolResourceUsage(
                model_calls=snapshot.model_calls + snapshot.descendant_model_calls,
                tool_calls=snapshot.tool_calls + snapshot.descendant_tool_calls,
                child_run_ids=tuple(dict.fromkeys((snapshot.run_id, *snapshot.child_run_ids))),
                **tokens,
            ),
        )

    async def _cancel_owned_result(
        self, child: ChildRunRequest, kernel: NativeKernel, *, code: str
    ) -> ToolResult:
        try:
            snapshot = await self._cancel(kernel, child)
        except StoreConflict:
            # The authorized parent-stop command can remain queued for the
            # current owner. Our old fence cannot advance its ledger.
            snapshot = kernel.snapshot(child.child_run_id)
            code = "child_lease_lost"
        return self._result(child, kernel, snapshot, code=code, uncertain=True)

    async def _run(
        self,
        child: ChildRunRequest,
        fingerprint: str,
        *,
        sealed_only: bool = False,
        recover_prepared: bool = False,
        resume_prepared: bool = False,
        resume_checkpoint: bool = False,
        on_ready: Callable[[NativeKernel, RunConfiguration, RunSnapshot], None] | None = None,
        on_interaction: Callable[[NativeKernel, RunSnapshot], Awaitable[None]] | None = None,
    ) -> ToolResult:
        bindings, kernel, owned = None, None, False
        terminal_proven = False
        query_failed = False
        result = None
        try:
            bindings = self.factory(child)
            if inspect.isawaitable(bindings):
                bindings = await bindings
            if not isinstance(bindings, ChildBindings):
                raise ValueError("child_bindings_invalid")
            # Fail closed before any child model work when the model asked for
            # parent media by scoped reference but the host factory did not
            # resolve it into a typed user input. A legacy or custom factory
            # that ignores ``media_refs`` must not silently drop the requested
            # image as plain text; the child never starts, so no HTTP dispatch
            # and no silent text fallback. Empty refs retain the original
            # text-only identity and need no resolved input.
            if child.media_refs and bindings.user_input is None:
                raise ValueError("child_media_ref_unresolved")
            config = self._configuration(child, bindings)
            tools = bindings.tools
            if bindings.artifacts is not None and tools is not None:
                # Validate execution grants first. The reserved host reader
                # grants no workspace capability and still consumes child budget.
                config, tools = install_artifact_tool(
                    config, tools, bindings.artifacts, enable_search=bindings.artifact_search,
                    enable_catalog=bindings.artifact_catalog,
                    page_policy=bindings.artifact_page_policy,
                    search_policy=bindings.artifact_search_policy,
                )
            parent_contract_digest = (
                digest(bindings.parent_contract) if child.output_contract == "parent" else None
            )
            kernel = NativeKernel(
                store=bindings.store,
                scope=child.scope,
                configuration=config,
                model=bindings.model,
                tools=tools,
                artifacts=bindings.artifacts,
                verifier=bindings.verifier,
                workspace=bindings.workspace,
                context_planner=bindings.context_planner,
                context_port=bindings.context_port,
                hook_port=bindings.hook_port,
                workspace_revision=bindings.workspace_revision,
                lease_ttl_seconds=bindings.lease_ttl_seconds,
            )
            try:
                snapshot = bindings.store.load(child.scope, child.child_run_id)
            except KeyError:
                if sealed_only:
                    return ToolResult(
                        status="uncertain", content={"error_code": "child_result_not_found"}
                    )
                if child.continuation_of is not None:
                    from .native_child_result import NativeChildResultPort

                    reader = NativeChildResultPort(
                        scope=child.parent_scope, roles=(child.role,), store=bindings.store,
                    )
                    parent = bindings.store.load(child.parent_scope, child.parent_run_id)
                    resolved = reader._load_session_child(parent, child.continuation_of)
                    if resolved is None:
                        raise ValueError("child_followup_not_available")
                    source, source_parent = resolved
                    allocation = next((item for item in source_parent.child_budget_reservations
                                       if item.child_run_id == source.run_id), None)
                    if (
                        not source.phase.terminal
                        or source.session_id != child.child_session_id
                        or source_parent.run_id != child.continuation_parent_run_id
                        # Synchronous sources were accounted by their sealed
                        # native_delegate receipt rather than a reservation.
                        or (allocation is not None and allocation.settled_usage_digest is None)
                    ):
                        raise ValueError("child_followup_not_available")
                try:
                    snapshot = kernel.start(
                        goal=child.task,
                        session_id=child.child_session_id,
                        run_id=child.child_run_id,
                        origin={
                            "kind": "native_subagent",
                            "parent_run_id": child.parent_run_id,
                            "parent_tool_call_id": child.parent_tool_call_id,
                            "parent_operation_key": child.parent_operation_key,
                            "parent_scope": child.parent_scope.model_dump(mode="json"),
                            "child_request_digest": fingerprint,
                            "role": child.role.role_id,
                            **({"workspace_access": "isolated_write",
                                "workspace_isolation_id": bindings.isolation_id,
                                "workspace_base_revision": bindings.workspace_revision}
                               if bindings.workspace_access == "isolated_write" else {}),
                            **({"artifact_policy": (
                                "scoped-child-observations/v2" if bindings.artifact_search
                                else "scoped-child-observations/v1"
                            )}
                               if bindings.artifacts is not None and tools is not None else {}),
                            **({"artifact_page_policy_digest": bindings.artifact_page_policy.digest}
                               if bindings.artifacts is not None and tools is not None
                               and bindings.artifact_page_policy is not None else {}),
                            **(
                                {
                                    "output_contract_source": "parent-output-contract/v1",
                                    "parent_output_contract_digest": parent_contract_digest,
                                }
                                if parent_contract_digest is not None
                                else {}
                            ),
                            **(
                                {
                                    "lease_policy": "native.owner_lease/v1",
                                    "lease_ttl_seconds": bindings.lease_ttl_seconds,
                                }
                                if bindings.lease_ttl_seconds is not None
                                else {}
                            ),
                            **({"continuation_of": child.continuation_of,
                                "continuation_parent_run_id": child.continuation_parent_run_id}
                               if child.continuation_of is not None else {}),
                            **({"child_media_policy": "host-scoped-parent-user-input/v1"}
                               if child.media_refs else {}),
                        },
                        expected_parent_run_id=child.continuation_of,
                        pin_parent=child.continuation_of is not None,
                        user_input=bindings.user_input,
                    )
                except StoreConflict:
                    if child.continuation_of is None:
                        raise
                    return self._error("child_followup_source_not_current")
                owned = True
            else:
                if snapshot.origin.get("child_request_digest") != fingerprint:
                    raise ValueError("child_operation_conflict")
                if bindings.workspace_access == "isolated_write" and (
                    snapshot.origin.get("workspace_access") != "isolated_write"
                    or snapshot.origin.get("workspace_isolation_id") != bindings.isolation_id
                    or snapshot.origin.get("workspace_base_revision") != bindings.workspace_revision
                ):
                    raise ValueError("child_workspace_isolation_changed")
                if parent_contract_digest is not None and (
                    snapshot.origin.get("output_contract_source") != "parent-output-contract/v1"
                    or snapshot.origin.get("parent_output_contract_digest")
                    != parent_contract_digest
                ):
                    raise ValueError("child_parent_output_contract_changed")
                if sealed_only and (
                    snapshot.goal != child.task
                    or snapshot.session_id != child.child_session_id
                    or snapshot.origin.get("kind") != "native_subagent"
                    or snapshot.origin.get("parent_scope")
                    != child.parent_scope.model_dump(mode="json")
                    or snapshot.origin.get("parent_run_id") != child.parent_run_id
                    or snapshot.origin.get("parent_tool_call_id") != child.parent_tool_call_id
                    or snapshot.origin.get("parent_operation_key") != child.parent_operation_key
                    or snapshot.origin.get("role") != child.role.role_id
                ):
                    raise ValueError("child_operation_conflict")
                snapshot = kernel.snapshot(child.child_run_id)
                if recover_prepared and not snapshot.phase.terminal:
                    from .checkpoint_recovery import (
                        claim_checkpoint_execution,
                        continue_checkpoint_execution,
                    )
                    from .prepared_recovery import (
                        claim_prepared_execution,
                        claim_prepared_verification,
                        continue_prepared_execution,
                        finish_prepared_verification,
                    )

                    original = snapshot
                    claim = (
                        claim_checkpoint_execution
                        if resume_checkpoint
                        else claim_prepared_execution
                        if resume_prepared
                        else claim_prepared_verification
                    )
                    snapshot, owned = await claim(
                        kernel,
                        original,
                        ttl=bindings.lease_ttl_seconds,
                        timeout=config.limits.effect_timeout_seconds,
                    )
                    if owned:
                        proceed = (
                            continue_checkpoint_execution
                            if resume_checkpoint
                            else continue_prepared_execution
                            if resume_prepared
                            else finish_prepared_verification
                        )
                        snapshot = await proceed(kernel, snapshot, original)
                    if (
                        snapshot.goal_revision != 0
                        or snapshot.goal != original.goal
                        or snapshot.origin != original.origin
                        or snapshot.session_id != original.session_id
                    ):
                        raise ValueError("child_operation_conflict")
                if not snapshot.phase.terminal:
                    result = self._result(
                        child, kernel, snapshot, code="child_requires_host_recovery", uncertain=True
                    )
                    if not recover_prepared:
                        return result
                    terminal_proven = False
                else:
                    terminal_proven = True
            if owned and not recover_prepared and on_ready is not None:
                # Publish the independently persisted child before its first
                # model effect. The parent still commits its own startup receipt.
                on_ready(kernel, config, snapshot)
            if owned and not recover_prepared:
                snapshot = await kernel.run_until_idle(child.child_run_id)
                while (
                    snapshot.phase == RunPhase.AWAITING_INTERACTION
                    and on_interaction is not None
                ):
                    await kernel._leases.keep_alive(
                        child.child_run_id, on_interaction(kernel, snapshot)
                    )
                    snapshot = await kernel.run_until_idle(child.child_run_id)
            code = None
            if not snapshot.phase.terminal:
                if recover_prepared:
                    return result
                code = (
                    "child_approval_required"
                    if snapshot.phase == RunPhase.AWAITING_INTERACTION
                    else "child_paused"
                )
                snapshot = await self._cancel(kernel, child)
            elif snapshot.phase != RunPhase.COMPLETED or snapshot.outcome in {
                "budget_exhausted",
                "blocked",
                "failed",
            }:
                code = "child_incomplete"
            result = self._result(child, kernel, snapshot, code=code)
        except asyncio.CancelledError:
            if (
                owned
                and kernel is not None
                and (not recover_prepared or resume_prepared or resume_checkpoint)
            ):
                canceled = await _settle(
                    self._cancel_owned_result(child, kernel, code="child_canceled"),
                    propagate_cancel=False,
                )
                self._results[child.child_run_id] = (fingerprint, canceled)
            raise
        except StoreConflict:
            query_failed = sealed_only
            result = (
                self._result(
                    child,
                    kernel,
                    kernel.snapshot(child.child_run_id),
                    code="child_lease_lost",
                    uncertain=True,
                )
                if kernel is not None
                else self._error("child_factory_failed")
            )
        except ValueError as error:
            query_failed = sealed_only
            # Only known local configuration errors cross the public boundary.
            codes = {
                "child_capability_grant_mismatch",
                "child_workspace_grant_mismatch",
                "child_workspace_policy_required",
                "child_workspace_isolation_required",
                "child_bindings_invalid",
                "child_operation_conflict",
                "child_output_contract_not_bound",
                "child_output_format_conflicts_with_binding",
                "child_parent_output_contract_unavailable",
                "child_parent_output_contract_not_bound",
                "child_parent_output_contract_changed",
                "child_followup_not_available",
                "child_media_ref_unauthorized",
                "child_media_ref_not_image",
                "child_media_ref_invalid",
                "child_media_ref_unresolved",
                "child_user_image_input_unsupported",
            }
            result = self._error(
                str(error) if str(error) in codes else "child_configuration_invalid"
            )
            if (
                owned
                and kernel is not None
                and (not recover_prepared or resume_prepared or resume_checkpoint)
            ):
                result = await self._cancel_owned_result(
                    child, kernel, code="child_execution_failed"
                )
        except Exception:
            query_failed = sealed_only
            if (
                owned
                and kernel is not None
                and (not recover_prepared or resume_prepared or resume_checkpoint)
            ):
                result = await self._cancel_owned_result(
                    child, kernel, code="child_execution_failed"
                )
            else:
                result = self._error("child_factory_failed")
        finally:
            if owned and kernel is not None:
                try:
                    kernel.release(child.child_run_id)
                except StoreConflict:
                    result = self._result(
                        child,
                        kernel,
                        kernel.snapshot(child.child_run_id),
                        code="child_lease_lost",
                        uncertain=True,
                    )
            if isinstance(bindings, ChildBindings) and bindings.cleanup is not None:
                try:
                    await _settle(bindings.cleanup())
                except Exception:
                    query_failed = sealed_only
                    if owned and kernel is not None:
                        result = self._result(
                            child,
                            kernel,
                            kernel.snapshot(child.child_run_id),
                            code="child_cleanup_failed",
                            uncertain=True,
                        )
                    else:
                        result = self._error("child_cleanup_failed")
        if sealed_only and (query_failed or not terminal_proven):
            # A rejected query is not evidence that the original operation
            # failed. The parent must retain its unknown-outcome boundary.
            return ToolResult(status="uncertain", content=result.content)
        return result

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        """Read a matching sealed child without starting or attaching any work.

        The current host factory must reproduce the original role, configuration
        and workspace binding. Process-local result caches cannot grant recovery.
        Parent counters are changed only when its Kernel seals this observation.
        """
        try:
            request = ToolRequest.model_validate_json(request.model_dump_json())
            child = self._request(request)
        except (ValueError, TypeError):
            return ToolResult(status="uncertain", content={"error_code": "child_query_rejected"})
        fingerprint = _digest(child.model_dump(mode="json"))
        async with self._locks.setdefault(child.child_run_id, asyncio.Lock()):
            result = await self._run(child, fingerprint, sealed_only=True)
            return ToolResult.model_validate_json(result.model_dump_json())

    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        """Recover only an original leased, prepared verification after owner expiry."""
        try:
            request = ToolRequest.model_validate_json(request.model_dump_json())
            child = self._request(request)
        except (ValueError, TypeError):
            return ToolResult(status="uncertain", content={"error_code": "child_query_rejected"})
        fingerprint = _digest(child.model_dump(mode="json"))
        async with self._locks.setdefault(child.child_run_id, asyncio.Lock()):
            result = await self._run(child, fingerprint, sealed_only=True, recover_prepared=True)
            return ToolResult.model_validate_json(result.model_dump_json())

    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        """Continue a matching leased child from a committed undispatched effect."""
        try:
            request = ToolRequest.model_validate_json(request.model_dump_json())
            child = self._request(request)
        except (ValueError, TypeError):
            return ToolResult(status="uncertain", content={"error_code": "child_query_rejected"})
        fingerprint = _digest(child.model_dump(mode="json"))
        async with self._locks.setdefault(child.child_run_id, asyncio.Lock()):
            result = await self._run(
                child, fingerprint, sealed_only=True, recover_prepared=True, resume_prepared=True
            )
            return ToolResult.model_validate_json(result.model_dump_json())

    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        """Resume an authenticated safe child checkpoint under a separate host grant."""
        try:
            request = ToolRequest.model_validate_json(request.model_dump_json())
            child = self._request(request)
        except (ValueError, TypeError):
            return ToolResult(status="uncertain", content={"error_code": "child_query_rejected"})
        fingerprint = _digest(child.model_dump(mode="json"))
        async with self._locks.setdefault(child.child_run_id, asyncio.Lock()):
            result = await self._run(
                child, fingerprint, sealed_only=True, recover_prepared=True, resume_checkpoint=True
            )
            return ToolResult.model_validate_json(result.model_dump_json())

    async def execute(self, request: ToolRequest) -> ToolResult:
        try:
            request = ToolRequest.model_validate_json(request.model_dump_json())
            child = self._request(request)
        except (ValueError, TypeError) as error:
            safe_codes = {
                "child_scope_or_definition_mismatch",
                "child_arguments_invalid",
                "child_budget_exhausted",
            }
            return self._error(str(error) if str(error) in safe_codes else "child_request_rejected")
        fingerprint = _digest(child.model_dump(mode="json"))
        lock = self._locks.setdefault(child.child_run_id, asyncio.Lock())
        async with lock:
            prior = self._results.get(child.child_run_id)
            if prior is not None:
                if prior[0] != fingerprint:
                    return self._error("child_operation_conflict")
                return ToolResult.model_validate_json(prior[1].model_dump_json())
            result = await self._run(child, fingerprint)
            self._results[child.child_run_id] = (fingerprint, result)
            return ToolResult.model_validate_json(result.model_dump_json())
