"""Local Studio opt-in composition of a locked DSH Build into the Native loop.

Studio remains the authority for Build, session identity, tool policy and
transport lifecycle. Durable storage requires an explicit host opt-in. This
assembly never claims independent acceptance evidence it does not possess.
"""

from __future__ import annotations

import hashlib
import json as json
import math
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from ksadk.harness.artifact_adapters.native_store import NativeArtifactStorePort
from ksadk.harness.artifact_adapters.native_tool import install_artifact_tool
from ksadk.harness.artifact_store import ArtifactStore
from ksadk.harness.browser_adapters.native_vision import NativeVisionBrowser
from ksadk.harness.browser_adapters.native_vision import (
    VisionObservationContext as VisionObservationContext,
)
from ksadk.harness.diagnostic_adapters.contracts import LanguageServerProfile
from ksadk.harness.native.activation_contract import ActivationResourceRef, McpLeaseRef
from ksadk.harness.native.completion import AcceptanceItem as AcceptanceItem
from ksadk.harness.native.completion import TaskContract
from ksadk.harness.native.context_runtime import CompactionPolicy as CompactionPolicy
from ksadk.harness.native.context_runtime import ContextPlanner
from ksadk.harness.native.delivery_review_contract import DeliveryReviewPolicy
from ksadk.harness.native.hook_executor import HookAuthorizer
from ksadk.harness.native.model import ModelProfile
from ksadk.harness.native.model_replay import project_model_message as project_model_message
from ksadk.harness.native.ports import (
    CHILD_RECOVERY_POLICIES,
    ChildRecoveryPolicy,
    RunConfiguration,
    RunLimits,
    ToolPort,
    VerificationPort,
)
from ksadk.harness.native.ports import ToolRequest as ToolRequest
from ksadk.harness.native.ports import ToolResult as ToolResult
from ksadk.harness.native.ports import WorkspaceObservationPolicy as WorkspaceObservationPolicy
from ksadk.harness.native.state import RunScope as RunScope
from ksadk.harness.native.studio_admission import StudioAdmission, studio_mapping_digest
from ksadk.harness.native_facade import NativeHarnessEngine
from ksadk.harness.native_runtime import NativeRuntimeAdapter, TaskContractInput
from ksadk.harness.progress_adapters.native_todo import install_todo_tool
from ksadk.harness.skill_adapters.native_disclosure import install_native_skill_tools
from ksadk.harness.skill_intent import is_skill_binding_question
from ksadk.harness.skill_runtime import SKILL_CATALOG_TOOL
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.harness.stores.native_contract import RunStore
from ksadk.harness.stores.native_memory import MemoryRunStore
from ksadk.harness.subagent_adapters.native_child_result import NativeChildTasksPort
from ksadk.plugins.providers.harness import HarnessEngineActivationContext
from ksadk.plugins.providers.harness_contributions import mcp_runtime
from ksadk.studio.contracts import ContextSpec, ExecutionSpec, MemorySpec
from ksadk.studio.model_client import OpenAICompatibleModelClient
from ksadk.studio.native_acceptance import StudioNativeAcceptanceExtension, extend_contract
from ksadk.studio.native_approval import StudioNativeApprovalAuthorizer
from ksadk.studio.native_background_command_port import StudioBackgroundCommandPort
from ksadk.studio.native_background_connection import BackgroundHostDeclaration
from ksadk.studio.native_background_ownership import NativeBackgroundOwnershipVerifier
from ksadk.studio.native_browser_port import StudioNativeBrowserPort
from ksadk.studio.native_child_mcp import StudioChildMcp
from ksadk.studio.native_child_skills import StudioChildSkills
from ksadk.studio.native_child_workspace import StudioChildFileWorkspaces
from ksadk.studio.native_context_port import StudioNativeContextPort
from ksadk.studio.native_final_shape import ARTIFACT_VERIFIER_REF as ARTIFACT_VERIFIER_REF
from ksadk.studio.native_final_shape import ARTIFACT_VERIFIER_VERSION as ARTIFACT_VERIFIER_VERSION
from ksadk.studio.native_final_shape import BROWSER_VERIFIER_REF as BROWSER_VERIFIER_REF
from ksadk.studio.native_final_shape import BROWSER_VERIFIER_VERSION as BROWSER_VERIFIER_VERSION
from ksadk.studio.native_final_shape import EVIDENCE_VERIFIER_REF as EVIDENCE_VERIFIER_REF
from ksadk.studio.native_final_shape import EVIDENCE_VERIFIER_VERSION as EVIDENCE_VERIFIER_VERSION
from ksadk.studio.native_final_shape import StudioFinalShapeVerifier
from ksadk.studio.native_final_shape import (
    declared_value_submission_schema as declared_value_submission_schema,
)
from ksadk.studio.native_final_shape import (
    encode_exact_workspace_file as encode_exact_workspace_file,
)
from ksadk.studio.native_final_shape import (
    final_shape_contract_description as final_shape_contract_description,
)
from ksadk.studio.native_final_shape import (
    requested_exact_workspace_file as requested_exact_workspace_file,
)
from ksadk.studio.native_final_shape import requested_final_shape as requested_final_shape
from ksadk.studio.native_hook_port import StudioNativeHookPort
from ksadk.studio.native_hook_registration import validate_hook_authorization_policy
from ksadk.studio.native_inventory_disclosure import (
    EmptyInventoryDisclosure,
    StudioInventoryDisclosureVerifier,
)
from ksadk.studio.native_language_diagnostics import StudioNativeLanguageDiagnostics
from ksadk.studio.native_mcp_tool_port import build_native_mcp_tool_surface
from ksadk.studio.native_memory_acceptance import StudioNativeMemoryVerifier, extend_memory_goal
from ksadk.studio.native_memory_flush import with_native_memory_flush
from ksadk.studio.native_memory_tool_port import build_native_memory_tools
from ksadk.studio.native_memory_turn import build_native_memory_turn_consumer
from ksadk.studio.native_model_port import (
    StudioNativeModelPort,
    native_model_profile_from_build,
    native_studio_streaming_support,
)
from ksadk.studio.native_model_recovery import (
    StudioModelTakeoverDeclaration,
    admit_studio_port,
    bound_capability,
)
from ksadk.studio.native_planning import (
    _estimated_token_counter as _estimated_token_counter,
)
from ksadk.studio.native_planning import (
    _locked_background_wait_timeout_seconds as _locked_background_wait_timeout_seconds,
)
from ksadk.studio.native_planning import (
    _locked_browser_timeout_seconds as _locked_browser_timeout_seconds,
)
from ksadk.studio.native_planning import (
    _native_effect_timeout_seconds as _native_effect_timeout_seconds,
)
from ksadk.studio.native_planning import (
    _studio_message_budget as _studio_message_budget,
)
from ksadk.studio.native_planning import (
    _studio_model_message_budget as _studio_model_message_budget,
)
from ksadk.studio.native_planning import (
    build_studio_context_planner,
)
from ksadk.studio.native_read_budget import StudioSourceReadPolicy
from ksadk.studio.native_scope import native_scope_from_activation
from ksadk.studio.native_session_context_port import build_session_context_tool
from ksadk.studio.native_skill_source import StudioNativeSkillSource
from ksadk.studio.native_subagent_port import (
    build_studio_native_subagent_port,
    required_subagent_effect_timeout,
)
from ksadk.studio.native_test_action import (
    TEST_ACTION_TOOL_NAME,
    StudioNativeTestActionVerifier,
    bind_test_action_scope,
    extend_with_test_action,
    install_required_test_tool,
)
from ksadk.studio.native_tool_port import build_native_locked_tool_surface
from ksadk.studio.native_workspace_port import StudioNativeWorkspacePort
from ksadk.toolsets.workspace_identity import identity_workspace_root
from ksadk.toolsets.workspace_read_budget import estimated_source_units as estimated_source_units

from .native_product_contracts import (
    _activation_contract as _activation_contract,
)
from .native_product_contracts import (
    _goal_contract as _goal_contract,
)
from .native_product_contracts import (
    _vision_observation_context as _vision_observation_context,
)
from .native_product_contracts import (
    _wall_delivery_seconds as _wall_delivery_seconds,
)
from .native_product_contracts import (
    studio_web_observation_policy as studio_web_observation_policy,
)

studio_web_observation_policy.__module__ = __name__


def _build_terminate_kill(
    declaration: BackgroundHostDeclaration | None,
) -> Callable[[str], bool] | None:
    """Preserve the host callback factory used by Studio integrations."""
    return build_terminate_kill(declaration)


from .native_tool_mux import NativeToolMux


class _ToolMux(NativeToolMux):
    """Stable Studio tool-mux identity backed by the bound-port dispatcher."""


def _studio_context_planner(
    profile: ModelProfile, *, context: ContextSpec | None = None,
    summary_output_policy=None,
    delivery_feedback_policy=None,
) -> ContextPlanner:
    return build_studio_context_planner(
        profile, context=context, planner_factory=ContextPlanner,
        summary_output_policy=summary_output_policy,
        delivery_feedback_policy=delivery_feedback_policy,
    )


from .native_background_owner import StudioBackgroundOwner, build_terminate_kill


class StudioNativeProductComposer(StudioBackgroundOwner):
    """One scoped Store per Studio workspace; one adapter per locked activation."""

    def __init__(
        self,
        *,
        model_client: OpenAICompatibleModelClient,
        workspace_root: Path,
        acceptance_extension: StudioNativeAcceptanceExtension | None = None,
        delivery_review_policy: DeliveryReviewPolicy | None = None,
        durable: bool = False,
        session_context_read: bool = False,
        browser_reobserve: bool = False,
        terminal_child_reconcile: bool = False,
        prepared_child_recovery: bool = False,
        child_recovery_policy: ChildRecoveryPolicy | None = None,
        hook_authorizer: HookAuthorizer | None = None,
        hook_authorization_version: str | None = None,
        diagnostic_profiles: Mapping[str, tuple[LanguageServerProfile, ...]] | None = None,
        model_recovery_policy: str | None = None,
        model_recovery_host: StudioModelTakeoverDeclaration | None = None,
        project_instruction_trust=None,
        background_host_declaration: BackgroundHostDeclaration | None = None,
        ownership_verifier: NativeBackgroundOwnershipVerifier | None = None,
    ) -> None:
        validate_hook_authorization_policy(hook_authorizer, hook_authorization_version)
        self._model_client = model_client
        self._workspace_root = workspace_root.resolve()
        self._project_instruction_trust = project_instruction_trust
        self._acceptance_extension = acceptance_extension
        self._delivery_review_policy = delivery_review_policy
        self._hook_authorizer = hook_authorizer
        self._hook_authorization_version = hook_authorization_version
        self._diagnostic_profiles = {
            agent: tuple(profile.model_dump_json() for profile in profiles)
            for agent, profiles in (diagnostic_profiles or {}).items()
        }
        self._durable = durable
        if model_recovery_policy is not None and (
            model_recovery_policy != "pure-inference-takeover/v1" or not durable
            or model_recovery_host is None
        ):
            raise ValueError("model takeover requires explicit durable host declaration")
        self._model_recovery_policy = model_recovery_policy
        self._model_recovery_host = model_recovery_host
        self._session_context_read = session_context_read
        self._browser_reobserve = browser_reobserve
        if child_recovery_policy is not None and (
            type(child_recovery_policy) is not str
            or child_recovery_policy not in CHILD_RECOVERY_POLICIES
        ):
            raise ValueError("Native child recovery policy is invalid")
        if (
            terminal_child_reconcile or prepared_child_recovery or child_recovery_policy
        ) and not durable:
            raise ValueError("Native child reconciliation requires durable storage")
        if (terminal_child_reconcile and prepared_child_recovery) or (
            child_recovery_policy and (terminal_child_reconcile or prepared_child_recovery)
        ):
            raise ValueError("select one Native child recovery policy")
        self._terminal_child_reconcile = terminal_child_reconcile
        self._prepared_child_recovery = prepared_child_recovery
        self._child_recovery_policy = child_recovery_policy or (
            "prepared_local_child/v1"
            if prepared_child_recovery
            else "sealed_local_child/v1"
            if terminal_child_reconcile
            else None
        )
        self.store: RunStore
        if durable:
            from ksadk.harness.stores.native_sqlite import SQLiteRunStore

            state_root = self._workspace_root / ".agentkit" / "plugin-runtime" / "state"
            state_root.mkdir(parents=True, exist_ok=True)
            self.store = SQLiteRunStore(state_root / "native-harness.sqlite")
        else:
            self.store = MemoryRunStore()
        # Content storage is separate from the Run ledger. Only the typed refs
        # committed by Native result governance can authorize a later read.
        # Keep it outside the editable workspace so tool writes cannot advance
        # or replace its index. One composer owns and closes the connection.
        artifact_root = (
            self._workspace_root / ".agentkit" / "plugin-runtime" / "state" / "native-artifacts"
        )
        artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifact_root.chmod(0o700)
        self._artifact_store = ArtifactStore(artifact_root)
        # Host-declared typed E2B background connection (target + credential
        # resolver). None means the E2B background capability is not declared
        # for this workspace: launch/query/stop/cold/delete all report unknown
        # with zero backend calls rather than resolving an env-ambient identity.
        # The host (Studio service) constructs this from a declared resource
        # connection; tests inject a fake declaration directly.
        self._background_declaration = background_host_declaration
        # Canonical Native ownership verifier: proves a frozen sandbox_id was
        # launched by a canonical Native run owned by the session (via the
        # committed launch receipt + sealed BackgroundCommandRef), reusing the
        # trusted path BackgroundCommandLocator traverses. None means the
        # verifier is not supplied — terminate_owner then cannot prove canonical
        # Native ownership and returns unknown for any existing sandbox (zero
        # backend calls). The Studio service constructs the concrete verifier
        # from event_store + session_service + this Composer's durable RunStore;
        # the Composer alone has no trusted session/run input.
        self._ownership_verifier = ownership_verifier

    def _owner_termination_kill(self):
        """Construct the host callback through the original Studio patch seam."""
        return _build_terminate_kill(self._background_declaration)

    async def aclose(self) -> None:
        try:
            self.store.close()
        finally:
            self._artifact_store.close()


    async def __call__(self, context: HarnessEngineActivationContext) -> NativeRuntimeAdapter:
        claims = dict(context.activation_claims)
        scope = native_scope_from_activation(context)
        if not context.activation_generation or context.activation_available is None:
            raise ValueError("native_studio_activation_generation_unavailable")
        activation_resource = ActivationResourceRef(
            scope=scope,
            activation_key=context.activation_key,
            generation=context.activation_generation,
            profile_digest=context.profile_digest,
            plugin_lock_digest=context.plugin_lock_digest,
            mcp_leases=tuple(
                McpLeaseRef(
                    name=pin.name,
                    generation=pin.generation,
                    descriptor_digest=pin.descriptor_digest,
                )
                for pin in context.mcp_resource_pins
            ),
        )

        async def activation_available() -> bool:
            if context.activation_available() is not True:
                return False
            return context.mcp_available is None or await context.mcp_available() is True

        build = context.bundle.resolved_agent_spec
        if (build.get("modelFailoverMode") or build.get("model_failover_mode")) == "automatic":
            # A fallback must own a locked profile, context budget and audited
            # model attempt. Do not silently bind only the primary or hide
            # cross-model retries inside the single-profile model port.
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "NATIVE_MODEL_FAILOVER_UNSUPPORTED",
                "Native 暂不支持自动切换候选模型，请将模型故障策略设为严格模式。",
                status_code=422,
                field="spec.bindings.modelFailoverMode",
            )
        model_name = claims.get("model")
        if not model_name or not claims.get("build_id"):
            raise ValueError("native_studio_model_binding_unavailable")
        profile = native_model_profile_from_build(
            build,
            selected_model=model_name,
            build_id=claims["build_id"],
            reasoning_effort=claims.get("reasoning_effort") or None,
            streaming_support=lambda selected: native_studio_streaming_support(
                self._model_client, selected
            ),
        )
        model = StudioNativeModelPort.from_resolved_build(
            self._model_client,
            scope=scope,
            resolved_agent_spec=build,
            selected_model=model_name,
            profile_digest=profile.digest,
        )
        if self._model_recovery_host is not None:
            admit_studio_port(model, self._model_recovery_host)
        vision_raw = build.get("visionModel") or build.get("vision_model")
        vision_profile = None
        vision_model = None
        if vision_raw is not None:
            if not isinstance(vision_raw, Mapping) or not isinstance(vision_raw.get("model"), str):
                raise ValueError("native_studio_vision_model_binding_invalid")
            vision_name = vision_raw["model"]
            vision_profile = native_model_profile_from_build(
                build,
                selected_model=vision_name,
                build_id=claims["build_id"],
                streaming_support=lambda selected: native_studio_streaming_support(
                    self._model_client, selected
                ),
                model_role="vision",
            )
            if vision_profile.image_input_mode != "native":
                raise ValueError("native_studio_vision_model_image_input_required")
            vision_model = StudioNativeModelPort.from_resolved_build(
                self._model_client,
                scope=scope,
                resolved_agent_spec=build,
                selected_model=vision_name,
                profile_digest=vision_profile.digest,
                model_role="vision",
            )
        full_access = claims.get("tool_approval_mode") == "full"
        workspace = context.user_workspace_root or self._workspace_root
        diagnostic_profiles = tuple(
            LanguageServerProfile.model_validate_json(value)
            for value in self._diagnostic_profiles.get(scope.agent_id, ())
        )
        if diagnostic_profiles and "process:host-user" not in build.get("security", {}).get(
            "allowedPermissions", (),
        ):
            raise ValueError("native_language_diagnostics_require_host_process_permission")
        language_diagnostics = StudioNativeLanguageDiagnostics(
            root=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
            profiles=diagnostic_profiles,
        ) if diagnostic_profiles else None
        context_planner = _studio_context_planner(
            profile, context=ContextSpec.model_validate(build["context"]),
            delivery_feedback_policy=(
                "admitted-delivery-continuation/v1"
                if build["execution"].get("nativeWallDeliveryPolicy") == "deadline-delivery/v7"
                else None
            ),
            summary_output_policy=(
                "declared-summary-limit/v1"
                if build["execution"].get("nativeWallDeliveryPolicy") in {"deadline-delivery/v3", "deadline-delivery/v4", "deadline-delivery/v5", "deadline-delivery/v6", "deadline-delivery/v7"}
                else None
            ),
        )
        context_port = StudioNativeContextPort(
            context, scope, self.store, planner=context_planner,
            workspace_root=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
            project_root=workspace, project_instruction_trust=self._project_instruction_trust,
        )
        hook_port = StudioNativeHookPort(
            context, scope, self.store, authorize=self._hook_authorizer,
            authorization_version=self._hook_authorization_version,
        ) if context.hook_sources else None
        authorize = StudioNativeApprovalAuthorizer(
            scope=scope, store=self.store, full_access=full_access
        )

        effect_timeout_seconds = _native_effect_timeout_seconds(
            profile,
            vision_profile,
            browser_tool_seconds=_locked_browser_timeout_seconds(build),
            owned_wait_tool_seconds=_locked_background_wait_timeout_seconds(build),
        )
        effect_timeout_seconds = max(
            effect_timeout_seconds, required_subagent_effect_timeout(build)
        )
        hook_port = with_native_memory_flush(
            context, scope, self.store, profile=profile,
            effect_timeout_seconds=effect_timeout_seconds, hook_port=hook_port,
        )
        if hook_port is not None:
            effect_timeout_seconds = max(
                effect_timeout_seconds, math.ceil(hook_port.required_effect_timeout_seconds) + 1,
            )
        persistent_browser = None
        if _locked_browser_timeout_seconds(build):
            persistent_browser = StudioNativeBrowserPort(
                scope=scope,
                root=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
                evidence_root=workspace / ".harness-tools" / "state" / "browser-verification",
                runtime_id="studio-browser-"
                + hashlib.sha256(context.activation_key.encode()).hexdigest()[:24],
                reobserve_after_loss=self._browser_reobserve,
            )
        tool_result_max_bytes = RunLimits().max_tool_result_bytes
        source_read_policy = StudioSourceReadPolicy(
            profile, _estimated_token_counter, "studio-cjk-ascii-estimate-125/v1",
            partial(_studio_model_message_budget, profile=profile),
            message_projection_id="provider-message-fields/v1",
            retain_requested_source=True, artifact_only_fallback=True,
            archive_model_policy="archived-model-view/v5", artifact_pages=True,
            artifact_result_max_bytes=tool_result_max_bytes, context_planner=context_planner,
        )
        artifact_page_policy = source_read_policy.artifact_policy(
            self.store, workspace_revision="unavailable", workspace_tracking=True,
        )
        builtins = build_native_locked_tool_surface(
            bundle_root=context.bundle.root,
            resolved_agent_spec=build,
            workspace_root=workspace,
            scope=scope,
            store=self.store,
            authorize=authorize,
            image_input_mode=("native" if vision_profile is not None else profile.image_input_mode),
            persistent_browser=persistent_browser,
            effect_timeout_seconds=effect_timeout_seconds,
            mcp_server_names=frozenset(item.name for item in context.config.mcp_tools),
            source_read_policy=source_read_policy,
            language_diagnostics=language_diagnostics,
            background_declaration=self._background_declaration,
        )
        authorize.require_approval(builtins.approval_required)
        browser_verifier = (
            persistent_browser
            if persistent_browser is not None
            and (full_access or "browser_inspect_workspace" not in builtins.approval_required)
            else None
        )

        source = StudioNativeSkillSource(context)
        empty_skill_inventory = EmptyInventoryDisclosure("Skill") if not source.refs else None

        def task_contract(facts: TaskContractInput) -> TaskContract:
            binding_question = is_skill_binding_question(facts.goal)
            baseline = _goal_contract(facts)
            baseline = extend_memory_goal(
                facts.goal, baseline,
                provider_ref=MemorySpec.model_validate(build.get("memory") or {}).provider_ref,
            )
            if binding_question:
                # Availability is fixed by the locked Build. Workspace and
                # network tools cannot change it, including when the model
                # would otherwise choose to inspect an unrelated project.
                baseline = baseline.model_copy(
                    update={"allowed_tool_names": (SKILL_CATALOG_TOOL,) if source.refs else ()}
                )
                if empty_skill_inventory is not None:
                    baseline = empty_skill_inventory.extend(baseline)
            if self._acceptance_extension is not None:
                baseline = extend_contract(facts, baseline, self._acceptance_extension)
            baseline = extend_with_test_action(
                goal=facts.goal,
                contract=baseline,
                workspace_root=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
                has_command_tool=any(item.name == "run_command" for item in builtins.definitions),
            )
            if empty_skill_inventory is not None and binding_question:
                baseline = empty_skill_inventory.resolve(baseline, facts.goal)
            return bind_test_action_scope(
                baseline, bound_tool_names=tuple(item.name for item in configuration.tools),
            )

        browser_port: ToolPort = builtins.executor
        if vision_profile is not None and vision_model is not None:
            browser_port = NativeVisionBrowser(
                browser=builtins.executor,
                vision_model=vision_model,
                vision_profile=vision_profile,
                actions=None,
                observation_context=lambda request: _vision_observation_context(
                    request, scope=scope, store=self.store
                ),
            )
        ports: dict[str, ToolPort] = {
            definition.name: (
                browser_port
                if definition.name == "browser_inspect_workspace"
                else builtins.executor
            )
            for definition in builtins.definitions
        }
        definitions = list(builtins.definitions)
        if language_diagnostics is not None:
            definition, diagnostic_port = language_diagnostics.tool_surface(
                scope=scope, authorize=authorize,
            )
            if definition.name in ports:
                raise ValueError("native_studio_tool_name_duplicate")
            ports[definition.name] = diagnostic_port
            definitions.append(definition)
        memory_definitions, memory_port = build_native_memory_tools(
            context, scope=scope, store=self.store, authorize=authorize, planner=context_planner,
        )
        for definition in memory_definitions:
            if definition.name in ports:
                raise ValueError("native_studio_tool_name_duplicate")
            ports[definition.name] = memory_port
            definitions.append(definition)
        authorize.require_approval(item.name for item in memory_definitions if not item.read_only)
        memory_turn_consumer = build_native_memory_turn_consumer(
            context, scope, self.store, profile=profile,
            effect_timeout_seconds=effect_timeout_seconds,
            mutation_definition=next(
                (item for item in memory_definitions if item.name == "manage_memory"), None,
            ),
        )
        if self._session_context_read:
            binding, reader = build_session_context_tool(
                scope=scope, store=self.store, authorize=authorize
            )
            if binding.definition.name in ports:
                raise ValueError("native_studio_tool_name_duplicate")
            ports[binding.definition.name] = reader
            definitions.append(binding.definition)
        close_callbacks = [persistent_browser.aclose] if persistent_browser is not None else []
        if language_diagnostics is not None:
            close_callbacks.append(language_diagnostics.close)
        if context_port.definitions:
            close_callbacks.append(context_port.aclose)
        if hook_port is not None:
            close_callbacks.append(hook_port.aclose)
        try:
            child_mcp = None
            if context.config.mcp_tools:
                runtime, transports, bindings = mcp_runtime(
                    context.config.mcp_tools,
                    runtime_credentials=context.mcp_runtime_credentials,
                )
                assert runtime is not None
                close_callbacks.extend(transport.close for transport in transports)
                surface = await build_native_mcp_tool_surface(
                    runtime=runtime,
                    binding_refs=tuple(item.capability_ref for item in bindings),
                    scope=scope,
                    store=self.store,
                    authorize=authorize,
                    activation_available=context.mcp_available,
                )
                # MCP tool schemas do not declare a trustworthy side-effect
                # class. In interactive modes, gate every remote call through
                # the same exact-call Studio approval used for built-in tools.
                if not full_access:
                    authorize.require_approval(
                        definition.name for definition in surface.definitions
                        if not definition.read_only
                    )
                for definition in surface.definitions:
                    if definition.name in ports:
                        raise ValueError("native_studio_tool_name_duplicate")
                    ports[definition.name] = surface.executor
                    definitions.append(definition)
                child_mcp = StudioChildMcp(
                    scope=scope, surface=surface,
                    activation_binding_digest=studio_mapping_digest({
                        "activation_claims": claims,
                        "profile_digest": context.bundle.composition.profile_digest,
                        "plugin_lock_digest": context.bundle.composition.plugin_lock_digest,
                    }),
                )
            instructions = context.config.prompt
            if not source.refs:
                # The locked Build is the authority for Skill availability.
                # With an empty binding there is no disclosure operation to
                # perform, so keep impossible Skill tools off the model surface.
                instructions += (
                    '\n\n<runtime_skill_bindings source="locked_agent_build">\n'
                    "No Skills are bound to this locked Agent Build. "
                    "Workspace files cannot grant a Skill binding.\n"
                    "</runtime_skill_bindings>"
                )
            execution = ExecutionSpec.model_validate(build.get("execution") or {})
            # Missing only in legacy sealed Builds. New Builds carry an
            # explicit object even when neither count is bounded.
            native_limits = execution.native_limits
            model_call_budget = (native_limits.max_model_calls if native_limits is not None
                                 else min(100, execution.max_steps))
            tool_call_budget = (native_limits.max_tool_calls if native_limits is not None
                                else min(400, max(4, execution.max_steps * 4)))
            reserve_basis = (model_call_budget if native_limits is not None
                             else execution.max_steps)
            workspace_port = StudioNativeWorkspacePort(
                root=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
                scope=scope,
                store=self.store,
            )
            delegation = build_studio_native_subagent_port(
                resolved_agent_spec=build,
                scope=scope,
                store=self.store,
                model=model,
                profile=profile,
                definitions=builtins.definitions,
                ports=ports,
                workspace=workspace_port,
                effect_timeout_seconds=effect_timeout_seconds,
                context_planner=context_planner,
                artifacts_factory=lambda child_scope: NativeArtifactStorePort(
                    self._artifact_store, scope=child_scope
                ),
                artifact_search=True,
                artifact_catalog=True,
                artifact_page_policy=artifact_page_policy,
                source_read_policy=source_read_policy,
                max_tool_result_bytes=tool_result_max_bytes,
                has_bound_skills=bool(context.skills),
                child_skills=(StudioChildSkills(
                    scope=scope, store=self.store, source=source, authorize=authorize,
                    metadata_policy=execution.native_skill_metadata_policy,
                    activation_binding_digest=studio_mapping_digest({
                        "activation_claims": claims,
                        "profile_digest": context.bundle.composition.profile_digest,
                        "plugin_lock_digest": context.bundle.composition.plugin_lock_digest,
                    }),
                ) if source.refs else None),
                has_bound_mcp=bool(context.config.mcp_tools),
                child_mcp=child_mcp,
                approval_required=(builtins.approval_required if not full_access else frozenset()),
                file_workspaces=StudioChildFileWorkspaces(
                    source=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
                    writable_root=workspace,
                    storage=(
                        self._workspace_root / ".agentkit/plugin-runtime/state/child-workspaces"
                    ),
                    scope=scope, store=self.store, authorize=authorize,
                    tool_names=builtins.child_file_tool_names,
                ),
                model_client=self._model_client,
                build_id=claims["build_id"],
                streaming_support=lambda selected: native_studio_streaming_support(
                    self._model_client, selected
                ),
            )
            if delegation is not None:
                if delegation.file_merge_surface is not None:
                    merge_definition, merge_executor = delegation.file_merge_surface
                    if merge_definition.name in ports:
                        raise ValueError("native_studio_tool_name_duplicate")
                    authorize.require_approval((merge_definition.name,))
                    ports[merge_definition.name] = merge_executor
                    definitions.append(merge_definition)
                if delegation.definition().name in ports:
                    raise ValueError("native_studio_tool_name_duplicate")
                ports[delegation.definition().name] = delegation
                definitions.append(delegation.definition())
                background = delegation.background_port
                if background is None:
                    raise ValueError("native_studio_child_background_port_unavailable")
                for definition in background.definitions():
                    if definition.name in ports:
                        raise ValueError("native_studio_tool_name_duplicate")
                    ports[definition.name] = background
                    definitions.append(definition)
                close_callbacks.append(background.close)
                result_port = delegation.result_port
                if result_port is None:
                    raise ValueError("native_studio_child_result_port_unavailable")
                result_definition = result_port.definition()
                if result_definition.name in ports:
                    raise ValueError("native_studio_tool_name_duplicate")
                ports[result_definition.name] = result_port
                definitions.append(result_definition)
                tasks_port = NativeChildTasksPort(result_port=result_port)
                tasks_definition = tasks_port.definition()
                if tasks_definition.name in ports:
                    raise ValueError("native_studio_tool_name_duplicate")
                ports[tasks_definition.name] = tasks_port
                definitions.append(tasks_definition)
            configuration = RunConfiguration(
                guidance_policy="boundary-guide/v1",
                tool_error_recovery_policy=execution.tool_error_recovery_policy,
                tool_error_recovery_model_calls=execution.tool_error_recovery_model_calls,
                background_notification_policy=execution.native_background_notification_policy,
                output_recovery_policy="task-or-delivery/v1",
                child_lifecycle_policy="owned-background/v2" if delegation is not None else None,
                child_promotion_policy=("owned-child-promotion/v1"
                                        if delegation is not None else None),
                child_input_policy="owned-child-guide/v1" if delegation is not None else None,
                child_release_policy="owned-run-release/v1" if delegation is not None else None,
                child_notification_policy="ready-before-model/v1"
                if delegation is not None else None,
                child_approval_policy="owned-child-approval/v1"
                if delegation is not None else None,
                context_sources=context_port.definitions,
                hooks=hook_port.definitions if hook_port is not None else (),
                hook_stop_continuation_limit=(
                    hook_port.stop_continuation_limit if hook_port is not None else None
                ),
                host_binding_digest=studio_mapping_digest(
                    {
                        "activation_claims": claims,
                        **({"model_recovery_policy": self._model_recovery_policy,
                            "model_recovery_host": self._model_recovery_host.digest}
                           if self._model_recovery_host is not None else {}),
                        "profile_digest": context.bundle.composition.profile_digest,
                        "plugin_lock_digest": context.bundle.composition.plugin_lock_digest,
                        "final_delivery_policy": "studio-declared-value/v3",
                        "artifact_delivery_policy": "studio-scoped-tool-artifacts/v2",
                        "final_submission_choice_policy": "sealed-final-only-required/v1",
                        "test_action_scope_policy": "requested-contract-only/v1",
                        **(
                            {"memory_completed_turn_binding": memory_turn_consumer.binding_digest}
                            if memory_turn_consumer is not None else {}
                        ),
                        **(
                            {
                                "skill_binding_policy": "studio-bundle-skill-integrity/v1",
                                "skill_resource_policy": "locked-bundle-files/v1",
                                "skill_bindings": source.binding_claims,
                            }
                            if source.binding_claims else {}
                        ),
                        **(
                            {"browser_recovery_policy": "reobserve_after_receipt/v1"}
                            if self._browser_reobserve
                            else {}
                        ),
                        **(
                            {"tool_recovery_policy": self._child_recovery_policy}
                            if self._child_recovery_policy
                            else {}
                        ),
                    }
                ),
                # Model-assisted review is diagnostic, not verified delivery.
                # Its real task benefit must be established before rollout.
                delivery_review_policy=self._delivery_review_policy,
                tool_recovery_policy=self._child_recovery_policy,
                model_recovery_policy=self._model_recovery_policy,
                workspace_observation_policy=(
                    studio_web_observation_policy() if browser_verifier is not None else None
                ),
                profile=profile,
                # This configuration is an activation pin, not a user turn.
                # The trusted extension must see only real goals when it
                # compiles per-goal acceptance and tool authority.
                contract=_activation_contract(
                    context.activation_key, browser_available=browser_verifier is not None,
                ),
                tools=tuple(definitions),
                instructions=instructions,
                limits=RunLimits(
                    max_tool_result_bytes=tool_result_max_bytes,
                    max_wall_seconds=execution.timeout_seconds or None,
                    finalization_reserve_wall_seconds=(
                        _wall_delivery_seconds(
                            execution, effect_timeout_seconds=RunLimits().effect_timeout_seconds,
                        )
                    ),
                    delivery_context_recovery_policy=(
                        "delivery-window-summary/v2"
                        if execution.native_wall_delivery_policy in {"deadline-delivery/v4", "deadline-delivery/v5", "deadline-delivery/v6", "deadline-delivery/v7"}
                        else "committed-write-summary/v1"
                        if execution.native_wall_delivery_policy == "deadline-delivery/v3"
                        else None
                    ),
                    max_model_calls=model_call_budget,
                    max_tool_calls=tool_call_budget,
                    verification_reserve_model_calls=(
                        (6 if reserve_basis is None else min(6, max(1, reserve_basis // 5)))
                        if browser_verifier is not None
                        else 0
                    ),
                    verification_idle_tool_calls=4 if browser_verifier is not None else 0,
                    browser_observation_interval_tool_calls=(
                        12 if browser_verifier is not None else 0
                    ),
                    finalization_reserve_model_calls=(
                        2 if vision_profile is not None else 1
                    ) if reserve_basis is None else min(
                        2 if vision_profile is not None else 1, reserve_basis,
                    ),
                    max_output_restarts=(
                        1 if reserve_basis is None or reserve_basis >= 20 else 0
                    ),
                    effect_timeout_seconds=effect_timeout_seconds,
                ),
            )
            tools: ToolPort = _ToolMux(ports)
            configuration, tools = install_todo_tool(configuration, tools)
            if source.refs:
                configuration, tools = install_native_skill_tools(
                    configuration,
                    tools,
                    scope=scope,
                    source=source,
                    skill_refs=source.refs,
                    authorize=authorize,
                    store=self.store,
                    metadata_policy=execution.native_skill_metadata_policy,
                )
            if "run_command" in builtins.approval_required:
                authorize.require_approval((TEST_ACTION_TOOL_NAME,))
            configuration, tools = install_required_test_tool(
                configuration,
                tools,
                workspace_root=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
                scope=scope,
                store=self.store,
                command_binding=builtins.command_binding,
                authorize=authorize,
            )
            artifacts = NativeArtifactStorePort(self._artifact_store, scope=scope)
            configuration, tools = install_artifact_tool(
                configuration, tools, artifacts, enable_search=True, enable_catalog=True,
                page_policy=artifact_page_policy,
                search_policy=source_read_policy.artifact_search_policy(
                    self.store, workspace_revision="unavailable", workspace_tracking=True,
                ),
            )
            verifier: VerificationPort = StudioFinalShapeVerifier(
                scope,
                build_id=claims["build_id"],
                workspace_root=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
                store=self.store,
                browser=browser_verifier,
                require_visual_feedback=(
                    vision_profile is not None and browser_verifier is not None
                ),
            )
            verifier = StudioNativeMemoryVerifier(
                context=context, scope=scope, store=self.store, baseline=verifier,
                definitions=memory_definitions,
            )
            if self._acceptance_extension is not None:
                verifier = self._acceptance_extension.bind_verifier(
                    context=context, scope=scope, baseline=verifier
                )
                if not callable(getattr(verifier, "verify", None)):
                    raise ValueError("native_studio_acceptance_verifier_invalid")
            verifier = StudioNativeTestActionVerifier(
                scope=scope,
                store=self.store,
                baseline=verifier,
                workspace_root=identity_workspace_root(workspace / ".harness-tools" / "workspace"),
            )
            verifier = StudioInventoryDisclosureVerifier(
                scope=scope,
                baseline=verifier,
                disclosure=empty_skill_inventory,
            )
            takeover_capability = (bound_capability(model, self._model_recovery_host,
                                    configuration.host_binding_digest)
                                   if self._model_recovery_host is not None else None)
            engine = NativeHarnessEngine(
                scope=scope,
                configuration=configuration,
                model=model,
                tools=tools,
                artifacts=artifacts,
                verifier=verifier,
                # Built-in file tools use this identity-scoped data root;
                # Studio's own SQLite/event files live above it and must not
                # advance the Agent's workspace revision each turn.
                workspace=workspace_port,
                context_planner=context_planner,
                store=self.store,
                context_port=context_port if context_port.definitions else None,
                hook_port=hook_port,
                child_lifecycle_port=(delegation.background_port
                                      if delegation is not None else None),
                background_command_port=(StudioBackgroundCommandPort(
                    scope=scope, store=self.store,
                    workspace_root=identity_workspace_root(
                        workspace / ".harness-tools" / "workspace"
                    ),
                    command_state_root=workspace / ".harness-tools" / "background",
                    make_background_session=self._make_background_session(
                        identity_workspace_root(workspace / ".harness-tools" / "workspace"),
                        workspace / ".harness-tools" / "background",
                    ),
                ) if execution.native_background_notification_policy is not None else None),
                lease_ttl_seconds=30 if self._durable else None,
                browser_recovery=persistent_browser,
                activation_resource=activation_resource,
                activation_available=activation_available,
                model_recovery_capability=takeover_capability,
            )
            pin = hashlib.sha256(claims["build_id"].encode()).hexdigest()[:24]
            spec = HarnessSpec(
                agent_revision_ref=f"agent-revision://studio-{pin}@1",
                model=ModelBinding(
                    profile_ref=f"model-profile://{profile.profile_id}@{profile.profile_version}"
                ),
                prompt=PromptSpec(instructions=configuration.instructions),
            )
            admission = StudioAdmission(
                build_id=claims["build_id"],
                manifest_sha256=claims["manifest_sha256"],
                model=model_name,
                project_dir=claims["project_dir"],
                launch_config_digest=claims["launch_config_digest"],
                request_config_digest=claims["request_config_digest"],
            )
            return NativeRuntimeAdapter(
                spec,
                engine=engine,
                scope=scope,
                configuration=configuration,
                store=self.store,
                durable=self._durable,
                studio_admission=admission,
                task_contract_factory=task_contract,
                turn_completed_consumer=memory_turn_consumer,
                close_callbacks=tuple(close_callbacks),
            )
        except BaseException:
            for close in reversed(close_callbacks):
                await close()
            raise
