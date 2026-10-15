"""Explicit opt-in assembly for an existing 'harness' RuntimeRegistry slot.

Settings are the host's resolved rollout decision, not model/request config.
This module installs no default registry entry and performs no network, plugin
installation, lifecycle management or implicit fallback.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from ksadk.harness.artifact_adapters.native_tool import install_artifact_tool
from ksadk.harness.native.adapters import ToolAuthorizer, _snapshot
from ksadk.harness.native.completion import TaskContract
from ksadk.harness.native.context_runtime import ContextPlanner
from ksadk.harness.native.ports import (
    ArtifactPort,
    ModelPort,
    RunConfiguration,
    ToolPort,
    VerificationPort,
    WorkspacePort,
)
from ksadk.harness.native.state import Contract, RunScope
from ksadk.harness.native.studio_admission import StudioAdmission, studio_mapping_digest
from ksadk.harness.native_facade import NativeHarnessEngine
from ksadk.harness.native_runtime import NativeRuntimeAdapter, TaskContractInput
from ksadk.harness.native_shadow_inputs import ShadowInputs
from ksadk.harness.progress_adapters.native_todo import install_todo_tool
from ksadk.harness.skill_adapters.native_disclosure import install_native_skill_tools
from ksadk.harness.skill_runtime import SkillSource
from ksadk.harness.spec import HarnessSpec
from ksadk.harness.stores.native_contract import RunStore
from ksadk.harness.workspace_adapters.native_directory import LocalWorkspacePort
from ksadk.runtime import RuntimeAdapter, RuntimeLaunchContext


class NativeHarnessSettings(Contract):
    """Resolved, immutable flags. Platform hierarchy/authorization stays outside."""

    native_harness_enabled: bool = False
    native_harness_shadow: bool = False
    native_harness_default: bool = False
    native_harness_durable: bool = False
    native_harness_cloud_enabled: bool = False
    engine_kind: Literal["legacy", "native"] | None = None


@dataclass(frozen=True)
class NativeRuntimeBindings:
    scope: RunScope
    spec: HarnessSpec
    configuration: RunConfiguration
    model: ModelPort
    tools: ToolPort
    verifier: VerificationPort
    context_planner: ContextPlanner
    store: RunStore
    # Host declaration of the supplied store; not evidence of cloud/pod sharing.
    durable: bool = False
    lease_ttl_seconds: float | None = None
    workspace_revision: str = "unavailable"
    workspace: WorkspacePort | None = None
    artifacts: ArtifactPort | None = None
    studio_admission: StudioAdmission | None = None
    skill_source: SkillSource | None = None
    skill_refs: tuple[str, ...] = ()
    skill_authorizer: ToolAuthorizer | None = None
    task_contract_factory: (
        Callable[[TaskContractInput], TaskContract | Awaitable[TaskContract]] | None
    ) = None
    # Activation-owned external transports are released by PluginHost through
    # NativeRuntimeAdapter.close_all, after all run handles are detached.
    close_callbacks: tuple[Callable[[], Awaitable[None]], ...] = ()


class HarnessExecutionEngineFactory:
    """Callable RuntimeAdapter factory with an injected, lazy legacy branch.

    A DSH provider supplies its already authorized model definitions/instructions
    and a host ToolPort routing each request to run-scoped admission. The facade
    allocates run IDs at start, so an admission object pinned to an unrelated
    preallocated run ID cannot be reused here. PluginHost retains activation,
    generation, transport and lease disposal authority.
    """

    def __init__(
        self,
        *,
        settings: NativeHarnessSettings | None = None,
        legacy_factory: Callable[[RuntimeLaunchContext], RuntimeAdapter] | None = None,
        native_bindings: NativeRuntimeBindings | None = None,
        shadow_inputs: ShadowInputs | None = None,
        shadow_timeout_seconds: float = 120,
    ) -> None:
        self._settings = _snapshot(settings or NativeHarnessSettings())
        self._legacy_factory = legacy_factory
        self._shadow_inputs = _snapshot(shadow_inputs) if shadow_inputs is not None else None
        self._shadow_timeout = shadow_timeout_seconds
        self._bindings = (
            None
            if native_bindings is None
            else replace(
                native_bindings,
                scope=_snapshot(native_bindings.scope),
                spec=native_bindings.spec.model_copy(deep=True),
                configuration=_snapshot(native_bindings.configuration),
            )
        )

    @property
    def settings(self) -> NativeHarnessSettings:
        return _snapshot(self._settings)

    def __call__(self, context: RuntimeLaunchContext) -> RuntimeAdapter:
        if context.runtime_type != "harness":
            raise ValueError("Harness factory requires runtime_type=harness")
        settings = self._settings
        if settings.native_harness_shadow:
            return self._create_shadow(context)
        selected = settings.engine_kind or (
            "native" if settings.native_harness_default else "legacy"
        )
        if selected == "legacy":
            if self._legacy_factory is None:
                raise ValueError("legacy_factory_unavailable")
            # Only this host callback may import/assemble its chosen old engine.
            runtime = self._legacy_factory(context)
            if not isinstance(runtime, RuntimeAdapter):
                raise TypeError("legacy factory must return RuntimeAdapter")
            return runtime
        if not settings.native_harness_enabled:
            raise ValueError("native_harness_disabled")
        if context.deployment_mode != "local":
            # This local bridge has no distributed worker admission/lease proof.
            raise ValueError("native_cloud_execution_unavailable")
        bindings = self._bindings
        if bindings is None:
            raise ValueError("native_host_bindings_unavailable")
        admission = bindings.studio_admission
        if admission is not None and (
            str(context.project_dir.resolve()) != admission.project_dir
            or studio_mapping_digest(context.config) != admission.launch_config_digest
        ):
            raise ValueError("native_studio_launch_config_unbound")
        for name, method in (("model", "complete"), ("tools", "execute"), ("verifier", "verify")):
            if not callable(getattr(getattr(bindings, name), method, None)):
                raise ValueError(f"native_{name}_binding_unavailable")
        if not isinstance(bindings.context_planner, ContextPlanner):
            raise ValueError("native_context_planner_binding_unavailable")
        if not isinstance(bindings.store, RunStore):
            raise ValueError("native_store_binding_unavailable")
        if settings.native_harness_durable and not bindings.durable:
            raise ValueError("native_durable_store_unavailable")
        if any(not callable(close) for close in bindings.close_callbacks):
            raise ValueError("native_close_callback_invalid")
        configuration, tools = install_todo_tool(bindings.configuration, bindings.tools)
        if bindings.artifacts is not None and any(
            not callable(getattr(bindings.artifacts, method, None)) for method in ("write", "read")
        ):
            raise ValueError("native_artifact_binding_unavailable")
        configuration, tools = install_artifact_tool(configuration, tools, bindings.artifacts)
        if bindings.skill_source is None:
            if bindings.skill_refs or bindings.skill_authorizer is not None:
                raise ValueError("native_skill_source_unavailable")
        else:
            if bindings.skill_authorizer is None:
                raise ValueError("native_skill_authorizer_unavailable")
            configuration, tools = install_native_skill_tools(
                configuration,
                tools,
                scope=bindings.scope,
                source=bindings.skill_source,
                skill_refs=bindings.skill_refs,
                authorize=bindings.skill_authorizer,
                store=bindings.store,
            )
        workspace = bindings.workspace or LocalWorkspacePort(context.project_dir)
        engine = NativeHarnessEngine(
            scope=bindings.scope,
            configuration=configuration,
            model=bindings.model,
            tools=tools,
            verifier=bindings.verifier,
            context_planner=bindings.context_planner,
            store=bindings.store,
            workspace_revision=bindings.workspace_revision,
            lease_ttl_seconds=bindings.lease_ttl_seconds,
            workspace=workspace,
            artifacts=bindings.artifacts,
        )
        return NativeRuntimeAdapter(
            bindings.spec,
            engine=engine,
            scope=bindings.scope,
            configuration=configuration,
            store=bindings.store,
            durable=settings.native_harness_durable,
            studio_admission=admission,
            task_contract_factory=bindings.task_contract_factory,
            close_callbacks=bindings.close_callbacks,
        )

    def _create_shadow(self, context: RuntimeLaunchContext) -> RuntimeAdapter:
        from ksadk.harness.native.ports import digest
        from ksadk.harness.native_shadow import ReadOnlyShadowRuntimeAdapter
        from ksadk.harness.native_shadow_inputs import isolate_bindings

        settings = self._settings
        selected = settings.engine_kind or (
            "native" if settings.native_harness_default else "legacy"
        )
        if selected != "legacy" or settings.native_harness_durable:
            raise ValueError("native_shadow_requires_local_legacy_primary")
        if self._shadow_inputs is None or self._bindings is None or self._legacy_factory is None:
            raise ValueError("native_shadow_explicit_snapshot_bindings_required")
        # Shadow cannot bypass the build/config admission of the supplied model.
        admission = self._bindings.studio_admission
        if admission is not None and (
            str(context.project_dir.resolve()) != admission.project_dir
            or studio_mapping_digest(context.config) != admission.launch_config_digest
        ):
            raise ValueError("native_studio_launch_config_unbound")
        isolated = isolate_bindings(self._bindings, self._shadow_inputs)
        native = HarnessExecutionEngineFactory(
            settings=settings.model_copy(
                update={"native_harness_shadow": False, "engine_kind": "native"}
            ),
            native_bindings=isolated,
        )(context)
        primary = self._legacy_factory(context)
        if not isinstance(primary, RuntimeAdapter):
            raise TypeError("legacy factory must return RuntimeAdapter")
        return ReadOnlyShadowRuntimeAdapter(
            primary,
            native,
            input_digest=digest(self._shadow_inputs),
            timeout_seconds=self._shadow_timeout,
        )

    def create(self, context: RuntimeLaunchContext) -> RuntimeAdapter:
        return self(context)
