"""Bind Studio Build sub-agents to isolated Native child Runs.

Read roles share observations. Host-admitted SDK file write roles use an
actual independent directory and controlled parent merge. Child approvals route
through the original parent/child commands; arbitrary processes need isolation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, Literal

from ksadk.harness.artifact_adapters.native_page_budget import ArtifactPagePolicy
from ksadk.harness.native.completion import (
    AcceptanceItem,
    CompletionCandidate,
    TaskContract,
    VerificationItemResult,
    VerificationReport,
)
from ksadk.harness.native.context_runtime import ContextPlanner
from ksadk.harness.native.model import ModelProfile
from ksadk.harness.native.output_contract import object_output_schema, validate_submission_schema
from ksadk.harness.native.ports import (
    ArtifactPort,
    ModelPort,
    RunConfiguration,
    RunLimits,
    ToolDefinition,
    ToolPort,
    ToolRequest,
    ToolResult,
    VerificationBundle,
    VerificationRequest,
    WorkspacePort,
    digest,
)
from ksadk.harness.native.state import RunScope
from ksadk.harness.native.subagents import ChildObservation, ChildRole, ChildRunRequest
from ksadk.harness.spec import SubAgentBinding
from ksadk.harness.stores.native_contract import LeasedRunStore, RunStore
from ksadk.harness.subagent import SubAgentOutputValidationError, validate_subagent_output
from ksadk.harness.subagent_adapters.native_background_child import NativeBackgroundChildToolPort
from ksadk.harness.subagent_adapters.native_child import ChildBindings, NativeChildToolPort
from ksadk.harness.subagent_adapters.native_child_continuation import bind_child_continuation
from ksadk.harness.subagent_adapters.native_child_result import NativeChildResultPort
from ksadk.studio.native_child_mcp import StudioChildMcp
from ksadk.studio.native_child_skills import StudioChildSkills
from ksadk.studio.native_child_workspace import StudioChildFileWorkspaces
from ksadk.studio.native_final_shape import (
    final_shape_contract_description,
    json_object_submission_schema,
    requested_final_shape,
)

_READ_ONLY_WORKSPACE_TOOLS = frozenset(
    {"workspace_status", "list_workspace_files", "read_workspace_file", "search_workspace_files"}
)
_SCHEMA_VERIFIER_REF = "studio.child-output-schema"
_SCHEMA_VERIFIER_VERSION = "1"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _ChildOutputSchemaVerifier:
    """Verify the child's own final candidate before its parent receives it."""

    def __init__(self, *, schema: dict[str, Any], child_run_id: str) -> None:
        self._schema = schema
        self._schema_digest = _sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False))
        self._child_run_id = child_run_id

    async def verify(self, request: VerificationRequest) -> VerificationBundle:
        try:
            validate_subagent_output(request.final_text, self._schema)
            valid = True
        except SubAgentOutputValidationError:
            valid = False
        subject = _sha256(request.final_text)
        environment = _sha256(f"{self._child_run_id}\0{self._schema_digest}")
        evidence_ref = (
            f"studio-child-schema://{self._schema_digest}/{_sha256(self._child_run_id)}/{subject}"
        )
        return VerificationBundle(
            candidate=CompletionCandidate(
                candidate_id=request.candidate_id,
                final_text=request.final_text,
                subject_digest=subject,
                subject_context_digest=request.subject_context_digest,
                environment_digest=environment,
            ),
            report=VerificationReport(
                candidate_id=request.candidate_id,
                contract_revision=request.contract.revision,
                subject_digest=subject,
                subject_context_digest=request.subject_context_digest,
                environment_digest=environment,
                items=(
                    VerificationItemResult(
                        acceptance_id="delegated_output_schema",
                        status="passed" if valid else "failed",
                        verifier_id=_SCHEMA_VERIFIER_REF,
                        verifier_version=_SCHEMA_VERIFIER_VERSION,
                        evidence_refs=(evidence_ref,),
                        verification_method="deterministic",
                        safe_summary=(
                            "Child output matches its bound JSON Schema."
                            if valid
                            else "Return only JSON matching the bound output schema."
                        ),
                    ),
                    VerificationItemResult(
                        acceptance_id="delegated_task",
                        status="not_run",
                        verifier_id="studio.child-independent-evidence-unavailable",
                        verifier_version="1",
                        evidence_refs=(),
                        verification_method="human",
                        safe_summary=(
                            "No independent evidence verifies the delegated task's meaning."
                        ),
                    ),
                ),
                repairable=not valid,
                failure_fingerprint=(
                    _sha256(f"invalid-child-schema\0{self._schema_digest}") if not valid else ""
                ),
                workspace_dependency="none",
            ),
        )


def _thaw_build_value(value: Any) -> Any:
    """Copy the plugin host's immutable Build view for strict schema validation."""
    if isinstance(value, Mapping):
        return {key: _thaw_build_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_build_value(child) for child in value]
    return value


def _bindings_from_build(resolved_agent_spec: Mapping[str, Any]) -> tuple[SubAgentBinding, ...]:
    raw = resolved_agent_spec.get("subAgents", resolved_agent_spec.get("sub_agents", ()))
    if not isinstance(raw, (list, tuple)):
        raise ValueError("native_studio_subagent_binding_invalid")
    return tuple(SubAgentBinding.model_validate(_thaw_build_value(item)) for item in raw)


def required_subagent_effect_timeout(resolved_agent_spec: Mapping[str, Any]) -> float:
    bindings = _bindings_from_build(resolved_agent_spec)
    if not bindings:
        return 0.0
    maximum = max(item.timeout_seconds for item in bindings)
    if maximum > 3585:
        raise ValueError("native_studio_subagent_timeout_exceeds_effect_lease")
    return maximum + 15.0


class _ChildToolMux:
    def __init__(self, ports: Mapping[str, ToolPort]) -> None:
        self._ports = dict(ports)

    async def execute(self, request: ToolRequest) -> ToolResult:
        port = self._ports.get(request.call.name)
        if port is None:
            return ToolResult(status="error", content={"error_code": "child_tool_not_granted"})
        return await port.execute(request)


class _StudioChildPort(NativeChildToolPort):
    """Pin dual-mode delivery to the host's locked role assembly."""

    def __init__(self, *, assembly_digest: str, store: RunStore | None = None,
                 file_workspace_policy: bool = False, borrowed_mcp_policy: bool = False, **kwargs):
        self._assembly_digest = assembly_digest
        self._continuation_store = store
        self._file_workspace_policy = file_workspace_policy
        self._borrowed_mcp_policy = borrowed_mcp_policy
        super().__init__(**kwargs)

    def definition(self) -> ToolDefinition:
        raw = super().definition().model_dump(mode="json")
        raw["feedback_profile_digest"] = self._assembly_digest
        raw["child_budget_policy"] = "delegate/v1"
        if self._borrowed_mcp_policy:
            raw["description"] += (
                " Inherited MCP tools use the parent activation's bound resources. "
                "Local file isolation does not isolate or roll back those effects; "
                "each call retains the child's exact authorization and approval."
            )
        raw["parameters"]["properties"]["background"] = {
            "type": "boolean", "default": False,
            "description": (
                "False waits for the findings; true returns the child ID and continues "
                "independent parent work. The host may move the same foreground child "
                "to the background. Collect background findings before final delivery."
            ),
        }
        raw["description"] += (
            " Foreground by default. A background startup is not findings; use "
            "native_child_control to send guidance, wait or stop the same child."
        )
        if self._file_workspace_policy:
            raw["description"] += (
                " File-writing roles modify an isolated copy, preserving current dirty "
                "and untracked inputs. Their delivery has not changed the parent files; "
                "use native_child_merge after collecting a completed child's result, "
                "then inspect and verify the parent workspace."
            )
        if self._continuation_store is not None:
            raw["parameters"]["properties"]["continuation_of"] = {
                "type": "string", "minLength": 1, "maxLength": 256,
                "description": (
                    "An accounted terminal child's run ID for a follow-up in its existing "
                    "Session. Collect background results first. Uses the current role and "
                    "a new turn and budget; the original result remains unchanged."
                ),
            }
        return ToolDefinition.model_validate(raw)

    def _request(self, request: ToolRequest) -> ChildRunRequest:
        if self._continuation_store is None or "continuation_of" not in request.call.arguments:
            return super()._request(request)
        arguments = dict(request.call.arguments)
        source_run_id = arguments.pop("continuation_of")
        child = super()._request(request.model_copy(update={
            "call": request.call.model_copy(update={"arguments": arguments}),
        }))
        return bind_child_continuation(
            child, source_run_id, store=self._continuation_store,
            roles=tuple(self.roles.values()),
        )

    @staticmethod
    def _configuration(child, bindings):
        config = NativeChildToolPort._configuration(child, bindings).model_dump(mode="json")
        if child.delivery_mode is not None:
            config["guidance_policy"] = "boundary-guide/v1"
        return RunConfiguration.model_validate(config)


class _StudioBackgroundChildPort(NativeBackgroundChildToolPort):
    def __init__(self, *, assembly_digest, **kwargs):
        self._assembly_digest = assembly_digest
        super().__init__(**kwargs)

    def definitions(self):
        return tuple(item.model_copy(update={"feedback_profile_digest": self._assembly_digest})
                     for item in super().definitions())


class StudioNativeSubagentPort:
    """Consume child-owned deadlines; legacy unbound ports keep their outer limit."""

    def __init__(
        self,
        *,
        child_port: NativeChildToolPort,
        bindings: Mapping[str, SubAgentBinding],
        result_port: NativeChildResultPort | None = None,
        background_port: NativeBackgroundChildToolPort | None = None,
        file_merge_surface: tuple[ToolDefinition, ToolPort] | None = None,
        active_execution_budget_bound: bool = False,
    ) -> None:
        self._child_port = child_port
        self._bindings = dict(bindings)
        self.result_port = result_port
        self.background_port = background_port
        self.file_merge_surface = file_merge_surface
        self._active_execution_budget_bound = active_execution_budget_bound

    def definition(self) -> ToolDefinition:
        return self._child_port.definition()

    async def execute(self, request: ToolRequest) -> ToolResult:
        return await self._invoke(request, action="execute")

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        return await self._invoke(request, action="reconcile")

    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        return await self._invoke(request, action="recover_prepared")

    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        return await self._invoke(request, action="resume_prepared")

    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        return await self._invoke(request, action="resume_checkpoint")

    async def _invoke(
        self,
        request: ToolRequest,
        *,
        action: Literal[
            "execute", "reconcile", "recover_prepared", "resume_prepared", "resume_checkpoint"
        ],
    ) -> ToolResult:
        role = request.call.arguments.get("role")
        binding = self._bindings.get(role) if isinstance(role, str) else None
        if binding is None:
            return ToolResult(
                status="error" if action == "execute" else "uncertain",
                content={"error_code": "child_role_not_bound"},
            )
        async def invoke():
            if action == "execute" and self.background_port is not None:
                return await self.background_port.execute_foreground(
                    request, delegate=self._child_port
                )
            return await getattr(self._child_port, action)(request)

        if self._active_execution_budget_bound:
            # The ordinary Native factory pins max_wall_seconds in the child
            # Kernel. A competing outer timer discards its sealed lineage and
            # usage when it cancels the owner just before that clock commits.
            # The parent's effect lease still bounds host assembly/cleanup.
            result = await invoke()
        else:
            try:
                async with asyncio.timeout(binding.timeout_seconds):
                    result = await invoke()
            except TimeoutError:
                return ToolResult(
                    status="uncertain", content={"error_code": "child_timeout_outcome_unknown"}
                )
        # A successful startup is an allocation receipt, not a final delivery.
        # The child keeps its locked output contract and verifier while owned
        # collection later consumes its actual terminal result.
        if (result.status != "ok" or result.child_budget_reservation is not None
                or binding.output_schema is None):
            return result
        try:
            observation = ChildObservation.model_validate(result.content)
        except ValueError:
            return ToolResult(
                status="error",
                content={"error_code": "child_observation_invalid"},
                resource_usage=result.resource_usage,
            )
        try:
            validate_subagent_output(observation.final_text, binding.output_schema)
        except SubAgentOutputValidationError:
            # Keep the child identity and accounting but do not let the parent
            # consume an invalid result as successful delegated evidence.
            content = observation.model_dump(mode="json")
            content.update(error_code="child_output_validation_failed", final_text="")
            return ToolResult(status="error", content=content, resource_usage=result.resource_usage)
        return result


def build_studio_native_subagent_port(
    *,
    resolved_agent_spec: Mapping[str, Any],
    scope: RunScope,
    store: RunStore,
    model: ModelPort,
    profile: ModelProfile,
    definitions: tuple[ToolDefinition, ...],
    ports: Mapping[str, ToolPort],
    workspace: WorkspacePort,
    effect_timeout_seconds: float,
    has_bound_skills: bool,
    has_bound_mcp: bool,
    approval_required: frozenset[str],
    context_planner: ContextPlanner | None = None,
    artifacts_factory: Callable[[RunScope], ArtifactPort] | None = None,
    artifact_search: bool = False,
    artifact_page_policy: ArtifactPagePolicy | None = None,
    source_read_policy=None,
    max_tool_result_bytes: int | None = None,
    file_workspaces: StudioChildFileWorkspaces | None = None,
    child_skills: StudioChildSkills | None = None,
    child_mcp: StudioChildMcp | None = None,
) -> StudioNativeSubagentPort | None:
    bindings = _bindings_from_build(resolved_agent_spec)
    if not bindings:
        return None
    by_name = {item.name: item for item in bindings}
    if len(by_name) != len(bindings):
        raise ValueError("native_studio_subagent_role_duplicate")
    by_tool = {item.name: item for item in definitions}
    writing_roles = set()
    skill_roles = {
        binding.name for binding in bindings if binding.inherit_skills and has_bound_skills
    }
    mcp_roles = {binding.name for binding in bindings if binding.inherit_mcp and has_bound_mcp}
    for binding in bindings:
        if (
            binding.max_total_tokens is not None
            or binding.depends_on
            or binding.failure_policy != "propagate"
            or binding.max_retries
        ):
            raise ValueError("native_studio_subagent_policy_unsupported")
        if (binding.name in skill_roles and child_skills is None) or (
            binding.name in mcp_roles and child_mcp is None
        ):
            raise ValueError("native_studio_subagent_capability_inheritance_unsupported")
        if (binding.name in mcp_roles and binding.concurrent_safe
                and any(not item.read_only for item in child_mcp.definitions)):
            raise ValueError("child_mcp_concurrent_effects_unsupported")
        if binding.timeout_seconds + 15 > effect_timeout_seconds:
            raise ValueError("native_studio_subagent_timeout_exceeds_effect_lease")
        if any(name in by_tool and not by_tool[name].read_only for name in binding.tools):
            if (
                file_workspaces is None or binding.concurrent_safe
                or any(name not in by_tool or name not in ports
                       or name not in file_workspaces.tool_names
                       for name in binding.tools)
            ):
                raise ValueError("native_studio_subagent_write_role_requires_isolation")
            writing_roles.add(binding.name)
        elif any(
            name not in _READ_ONLY_WORKSPACE_TOOLS
            or name not in by_tool
            or not by_tool[name].read_only
            or name not in ports
            for name in binding.tools
        ):
            raise ValueError("native_studio_subagent_write_role_requires_isolation")

    roles = tuple(ChildRole(
        role_id=item.name, description=item.description or item.name,
        tool_names=(*item.tools, *(child_skills.tool_names if item.name in skill_roles else ()),
                    *(child_mcp.tool_names if item.name in mcp_roles else ())),
        concurrent_safe=item.concurrent_safe,
        workspace_access="isolated_write" if item.name in writing_roles else "read_only",
    ) for item in bindings)

    async def factory(child: ChildRunRequest) -> ChildBindings:
        binding = by_name[child.role.role_id]
        parent = store.load(child.parent_scope, child.parent_run_id)
        schema = binding.output_schema
        declared_object = child.output_format == "json_object"
        if child.output_contract == "parent" and not parent.task_contract:
            raise ValueError("child_parent_output_contract_unavailable")
        parent_contract = (
            TaskContract.model_validate_json(json.dumps(parent.task_contract))
            if child.output_contract == "parent"
            else None
        )
        parent_output = (
            object_output_schema(
                parent_contract.final_submission_schema,
                parent_contract.final_submission_payload_key,
            )
            if parent_contract is not None
            else None
        )
        if child.output_contract == "parent" and parent_output is None:
            raise ValueError("child_parent_output_contract_unavailable")
        if declared_object and schema is not None and schema.get("type") != "object":
            raise ValueError("child_output_format_conflicts_with_binding")
        schema_text = (
            json.dumps(schema, sort_keys=True, ensure_ascii=False) if schema is not None else None
        )
        # Explicit Build schemas keep their existing child Verify→Repair path.
        # Otherwise, compile the delegated goal through the same host protocol
        # as an ordinary parent, rather than relying on role prose to enforce it.
        object_submission = (
            parent_output is not None
            or schema is None
            and (declared_object or requested_final_shape(child.task) == "json_object")
        )
        # An explicit caller type survives task rephrasing. A locked object
        # schema stays exact and remains subject to host submission limits.
        submission_schema = (
            schema
            if declared_object and schema is not None
            else json_object_submission_schema(child.task)
            if object_submission
            else None
        )
        if parent_output is not None:
            # The caller selects an existing host constraint, not a schema
            # supplied by model text. Locked role constraints stay conjunctive.
            inner = (
                {"type": "object", "allOf": [schema, parent_output]}
                if schema is not None
                else parent_output
            )
            submission_schema = {
                "type": "object",
                "properties": {"output": inner},
                "required": ["output"],
                "additionalProperties": False,
            }
            validate_submission_schema(submission_schema, "output")
        granted_definitions = tuple(by_tool[name] for name in binding.tools)
        granted_ports = {name: ports[name] for name in binding.tools}
        revision = hashlib.sha256(
            (
                f"{child.child_run_id}\0{child.task}"
                + ("\0output_format=json_object" if declared_object else "")
                + ("\0parent_output=" + digest(parent_contract) if parent_contract else "")
            ).encode("utf-8")
        ).hexdigest()[:24]
        contract_items = [
            AcceptanceItem(
                acceptance_id="delegated_task",
                verifier_ref="studio.child-independent-evidence-unavailable",
                verifier_version="1",
                verification_method="human",
                description=(
                    "Declared child delivery: one JSON object. Semantics remain unverified."
                    if declared_object
                    else final_shape_contract_description(child.task)
                    if object_submission
                    else child.task[:1024]
                ),
            ),
        ]
        if schema_text is not None:
            contract_items.append(
                AcceptanceItem(
                    acceptance_id="delegated_output_schema",
                    verifier_ref=_SCHEMA_VERIFIER_REF,
                    verifier_version=_SCHEMA_VERIFIER_VERSION,
                    verification_method="deterministic",
                    description="Child final output matches the bound JSON Schema.",
                )
            )
        contract = TaskContract(
            revision=f"studio-child-{revision}",
            items=tuple(contract_items),
            final_submission_schema=submission_schema,
            final_submission_payload_key="output" if object_submission else None,
        )
        configuration = RunConfiguration(
            profile=profile,
            contract=contract,
            tools=granted_definitions,
            instructions=(
                binding.instructions
                if schema_text is None
                else (
                    f"{binding.instructions}\n\n"
                    "The locked child output contract requires your final response to be only "
                    f"JSON matching this schema: {schema_text}"
                )
            ),
            limits=RunLimits(
                **({"max_tool_result_bytes": max_tool_result_bytes}
                   if max_tool_result_bytes is not None else {}),
                max_model_calls=binding.max_turns,
                max_repairs=min(2, max(0, binding.max_turns - 1)),
                max_tool_calls=(
                    binding.max_tool_calls
                    if binding.max_tool_calls is not None
                    else binding.max_turns * 4
                    if child.role.tool_names
                    else 0
                ),
                max_subagent_depth=0,
                effect_timeout_seconds=effect_timeout_seconds,
            ),
        )
        child_tools = (
            _ChildToolMux(granted_ports) if granted_ports or binding.name in skill_roles else None
        )
        if binding.name in skill_roles:
            configuration, child_tools = child_skills.bind(child, configuration, child_tools)
        if binding.name in mcp_roles:
            configuration, child_tools = child_mcp.bind(child, configuration, child_tools)
        child_workspace, child_revision, isolation_id = workspace, (
            parent.workspace_revision or "studio-workspace-unobserved"
        ), None
        if binding.name in writing_roles:
            isolated, child_tools, child_workspace = await file_workspaces.bind(child, child_tools)
            child_revision, isolation_id = isolated.base_revision, isolated.isolation_id
        return ChildBindings(
            store=store,
            configuration=configuration,
            model=model,
            verifier=(
                _ChildOutputSchemaVerifier(schema=schema, child_run_id=child.child_run_id)
                if schema is not None
                else None
            ),
            tools=child_tools,
            workspace=child_workspace,
            context_planner=context_planner,
            workspace_revision=child_revision,
            workspace_access=child.role.workspace_access,
            isolation_id=isolation_id,
            workspace_policy_bound=True,
            lease_ttl_seconds=30.0 if isinstance(store, LeasedRunStore) else None,
            parent_contract=parent_contract,
            artifacts=(
                artifacts_factory(child.scope)
                if artifacts_factory is not None and granted_ports else None
            ),
            artifact_search=artifact_search,
            artifact_search_policy=(
                source_read_policy.artifact_search_policy(
                    store, workspace_revision=(parent.workspace_revision
                                               or "studio-workspace-unobserved"),
                    workspace_tracking=workspace is not None,
                ) if source_read_policy is not None and artifact_search else None
            ),
            artifact_page_policy=(
                source_read_policy.artifact_policy(
                    store, workspace_revision=(parent.workspace_revision
                                               or "studio-workspace-unobserved"),
                    workspace_tracking=workspace is not None,
                ) if source_read_policy is not None else artifact_page_policy
            ),
        )

    assembly_digest = "sha256:" + _sha256(
        json.dumps(
            {
                "version": "studio-child-declared-delivery/v8",
                **({"version": "studio-child-declared-delivery/v10",
                    "artifact_page_policy": artifact_page_policy.digest,
                    "artifact_page_origin_binding": "sealed-child-origin/v1"}
                   if artifact_page_policy is not None else {}),
                **({"version": "studio-child-declared-delivery/v11",
                    "max_tool_result_bytes": max_tool_result_bytes}
                   if max_tool_result_bytes is not None else {}),
                **({"version": "studio-child-declared-delivery/v13",
                    "search_capacity_policy": "complete-next-search-request/v1",
                    "source_capacity_policy": source_read_policy.digest}
                   if source_read_policy is not None else {}),
                **({"artifact_policy": (
                    "scoped-child-observations/v2" if artifact_search
                    else "scoped-child-observations/v1"
                )}
                   if artifacts_factory is not None else {}),
                "bindings": [binding.model_dump(mode="json") for binding in bindings],
                **({"child_skill_policy": "studio-child-skill-knowledge/v1",
                    "child_skill_binding": child_skills.binding_digest}
                   if skill_roles else {}),
                **({"child_mcp_policy": "studio-child-borrowed-mcp/v1",
                    "child_mcp_binding": child_mcp.binding_digest}
                   if mcp_roles else {}),
                "background_policy": "studio-owned-background-child/v8",
                "child_input_policy": "owned-child-guide/v1",
                "child_approval_policy": "owned-child-approval/v1",
                "child_time_budget_policy": "active-child-clock/v1",
                "child_notification_policy": "ready-before-model/v1",
                "foreground_owner_policy": "shared-child-task-owner/v1",
                "delegate_delivery_policy": "foreground-or-background/v1",
                "delegate_continuation_policy": "parent-accounted-session-turn/v1",
                **({"child_workspace_policy": "studio-child-files/v2"} if writing_roles else {}),
                "background_guidance_policy": "boundary-guide/v1",
                "background_continuation_policy": "parent-accounted-session-turn/v1",
                "profile": profile.model_dump(mode="json"),
                "tools": [
                    by_tool[name].model_dump(mode="json")
                    for name in sorted({name for binding in bindings for name in binding.tools})
                ],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    async def background_factory(child):
        # Reuse the exact locked role's model, tool grants, workspace, Context
        # and verifier. Its wall limit belongs to the child Kernel, so returning
        # a startup receipt does not end the deadline or lose timeout accounting.
        bindings = await factory(child)
        config = bindings.configuration
        limits = config.limits.model_copy(update={
            "max_wall_seconds": by_name[child.role.role_id].timeout_seconds,
        })
        config = RunConfiguration.model_validate({**config.model_dump(), "limits": limits})
        return replace(bindings, configuration=config)

    child_port = _StudioChildPort(
        assembly_digest=assembly_digest,
        store=store,
        scope=scope,
        roles=roles,
        file_workspace_policy=bool(writing_roles),
        borrowed_mcp_policy=bool(mcp_roles),
        factory=background_factory,
    )

    background = _StudioBackgroundChildPort(
        scope=scope, roles=tuple(child_port.roles.values()), factory=background_factory,
        store=store, assembly_digest=assembly_digest,
        approval_bridge=True,
    )
    return StudioNativeSubagentPort(
        child_port=child_port,
        bindings=by_name,
        file_merge_surface=file_workspaces.merge_surface(roles) if writing_roles else None,
        result_port=NativeChildResultPort(
            scope=scope, store=store, roles=tuple(child_port.roles.values())
        ),
        background_port=background,
        active_execution_budget_bound=True,
    )
