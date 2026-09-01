"""Local Studio execution for immutable PluginHost AgentBundle v2 builds."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ksadk.harness.reasoner import (
    HarnessReasoner,
    HarnessReasoningTurn,
    HarnessToolCall,
)
from ksadk.plugins.builtins import (
    builtin_capability_factories,
    builtin_capability_manifests,
)
from ksadk.plugins.bundle import PluginBundleError, PluginBundleResolver, ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginHost, PluginHostError
from ksadk.plugins.providers.harness import (
    HarnessTurnResult,
    KsADKHarnessProviderFactory,
)
from ksadk.plugins.providers.legacy import (
    LegacyBundleAdapter,
    LegacyBundleCompatibilityError,
    LegacyHarnessSource,
)
from ksadk.plugins.providers.legacy_catalog import (
    KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
    builtin_agent_provider_manifests,
    legacy_harness_agent_provider_manifest,
)
from ksadk.plugins.resolver import PluginRegistry
from ksadk.runtime import RuntimeLaunchContext
from ksadk.sessions.base import BaseSessionService
from ksadk.studio.contracts import (
    BuildRecord,
    NetworkPolicy,
    ResolvedModel,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import OpenAICompatibleModelClient
from ksadk.studio.plugin_kernel_adapter import StudioPluginKernelAdapter
from ksadk.studio.repository import BuildRepository
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.workspace import Workspace

_COMPOSED_RUNTIME_TYPES = frozenset({"harness", "plugin"})


@dataclass(frozen=True)
class StudioPluginTurnResult:
    """Normalized result retained at the Studio/third-party boundary."""

    output_text: str
    session_id: str
    usage: Mapping[str, Any]
    metadata: Mapping[str, Any]
    raw: Any


@dataclass
class _HostEntry:
    agent_id: str
    bundle: ResolvedPluginBundle
    host: PluginHost


class _StudioHarnessReasoner:
    """Bind the Harness loop to the immutable Studio model/network policy."""

    def __init__(
        self,
        client: OpenAICompatibleModelClient,
        *,
        model: ResolvedModel,
        network_policy: NetworkPolicy,
        timeout_seconds: int,
        max_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self._client = client
        self._model = model
        self._network_policy = network_policy
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
    ) -> HarnessReasoningTurn:
        del prompt
        if model != self._model.model:
            raise PluginHostError(
                "harness_model_not_bound",
                f"Harness requested unbound model {model!r}",
            )
        response = await self._client.complete(
            self._model,
            messages=[dict(message) for message in messages],
            network_policy=self._network_policy,
            timeout_seconds=self._timeout_seconds,
            max_attempts=self._max_attempts,
            backoff_seconds=self._backoff_seconds,
            tools=[dict(tool.openai_schema) for tool in tools],
            allow_empty=bool(tools),
        )
        calls: list[HarnessToolCall] = []
        for call in response.tool_calls:
            try:
                arguments = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as error:
                raise PluginHostError(
                    "harness_tool_arguments_invalid",
                    f"model emitted invalid arguments for tool {call.name!r}",
                ) from error
            if not isinstance(arguments, dict):
                raise PluginHostError(
                    "harness_tool_arguments_invalid",
                    f"model emitted non-object arguments for tool {call.name!r}",
                )
            calls.append(
                HarnessToolCall(
                    call_id=call.id,
                    name=call.name,
                    arguments=arguments,
                )
            )
        return HarnessReasoningTurn(
            final_text=response.content or None,
            tool_calls=tuple(calls),
        )


class StudioPluginRuntime:
    """Resolve, activate, and retain composed providers for Studio sessions."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        build_repository: BuildRepository,
        session_service: BaseSessionService,
        model_client: OpenAICompatibleModelClient,
        secret_resolver: Any,
        harness_reasoner: HarnessReasoner | None = None,
        provider_manifests: Mapping[str, PluginManifest] | None = None,
        provider_factories: Mapping[str, Any] | None = None,
        legacy_harness_sources: Sequence[LegacyHarnessSource] = (),
    ) -> None:
        self.workspace = workspace
        self.builds = build_repository
        self._session_service = session_service
        self._model_client = model_client
        self._secret_resolver = secret_resolver
        self._harness_reasoner = harness_reasoner
        self._provider_manifests = dict(provider_manifests or {})
        self._provider_factories = dict(provider_factories or {})
        self._legacy_bundles = LegacyBundleAdapter(legacy_harness_sources)
        self._lock = asyncio.Lock()
        self._hosts: dict[str, _HostEntry] = {}

    def replace_provider_registrations(
        self,
        provider_manifests: Mapping[str, PluginManifest],
        provider_factories: Mapping[str, Any],
    ) -> None:
        """Bind one startup snapshot before any provider activation exists."""

        manifests = dict(provider_manifests)
        factories = dict(provider_factories)
        if manifests.keys() != factories.keys():
            raise ValueError(
                "plugin provider manifests and factories must use the same exact references"
            )
        if self._hosts:
            raise RuntimeError("cannot replace provider registrations after activation")
        self._provider_manifests = manifests
        self._provider_factories = factories

    @property
    def active_activation_count(self) -> int:
        return sum(entry.host.activation_count for entry in self._hosts.values())

    def resolve(self, build_id: str, *, model: str | None = None) -> StudioRunSpec:
        build = self.builds.get(build_id)
        runtime_type = build.runtime_type.strip().lower()
        if runtime_type not in _COMPOSED_RUNTIME_TYPES:
            raise StudioError(
                "BUILD_RUNTIME_UNSUPPORTED",
                "Build 不是 PluginHost Harness/Provider Runtime",
                status_code=422,
                details={"buildId": build_id, "runtimeType": runtime_type},
            )
        bundle_root = self._bundle_root(build)
        bundle = self._resolve_bundle(bundle_root)
        self._preflight_bundle(bundle)
        selected_model = self._select_model(build, model)
        resolved = bundle.resolved_agent_spec
        instructions = resolved.get("instructions")
        instructions = instructions if isinstance(instructions, Mapping) else {}
        return StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type=runtime_type,
                project_dir=bundle_root,
                config={"plugin_bundle_digest": bundle.bundle_digest},
            ),
            build_id=build.id,
            agent_id=build.agent_id,
            model=selected_model,
            request_config={
                "agent_system": str(instructions.get("system") or ""),
                "agent_task": str(instructions.get("task") or ""),
                "plugin_bundle_digest": bundle.bundle_digest,
            },
            manifest_sha256=build.resolved_digest,
            plugin_bundle_root=bundle_root,
        )

    async def execute(
        self,
        spec: StudioRunSpec,
        request: Mapping[str, Any],
        *,
        session_id: str,
    ) -> StudioPluginTurnResult:
        if spec.plugin_bundle_root is None:
            raise PluginHostError(
                "plugin_bundle_unavailable", "Studio run has no PluginHost Bundle"
            )
        entry = await self._host_for(spec.plugin_bundle_root)
        if entry.agent_id != spec.agent_id:
            raise PluginHostError(
                "plugin_bundle_agent_mismatch",
                "Studio run Agent does not match its immutable PluginHost Bundle",
            )
        activation = await entry.host.open_activation(
            entry.bundle,
            activation_key=session_id,
        )
        raw = await activation.execute(dict(request))
        return _normalize_result(raw, session_id=session_id)

    def kernel_adapter_provider(self, spec: StudioRunSpec):  # type: ignore[no-untyped-def]
        """Return a lazy, Build-pinned adapter factory for Scheduler Kernel."""

        if spec.plugin_bundle_root is None:
            raise StudioError(
                "PLUGIN_RUNTIME_UNAVAILABLE",
                "Studio Build 没有 PluginHost Bundle",
                status_code=409,
            )
        return lambda: StudioPluginKernelAdapter(self, spec)

    async def kernel_adapter(
        self,
        spec: StudioRunSpec,
        *,
        session_id: str,
    ) -> Any:
        """Bind a Scheduler run to the provider-owned activation adapter."""

        if spec.plugin_bundle_root is None:
            raise StudioError(
                "PLUGIN_RUNTIME_UNAVAILABLE",
                "Studio Build 没有 PluginHost Bundle",
                status_code=409,
            )
        entry = await self._host_for(spec.plugin_bundle_root)
        if entry.agent_id != spec.agent_id:
            raise PluginHostError(
                "plugin_bundle_agent_mismatch",
                "Studio run Agent does not match its immutable PluginHost Bundle",
            )
        activation = await entry.host.open_activation(
            entry.bundle,
            activation_key=session_id,
        )
        return await activation.runtime_adapter()

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            entries = tuple(self._hosts.values())
        for entry in entries:
            await entry.host.close_activation(session_id)

    async def aclose(self) -> None:
        async with self._lock:
            entries = tuple(self._hosts.values())
            self._hosts.clear()
        for entry in entries:
            await entry.host.dispose()

    async def _host_for(self, bundle_root: Path) -> _HostEntry:
        # Re-resolve every turn. This deliberately rechecks enabled receipts and
        # package digests rather than trusting a previously healthy child.
        bundle = self._resolve_bundle(bundle_root)
        key = bundle.bundle_digest
        async with self._lock:
            existing = self._hosts.get(key)
            if existing is not None:
                return existing

            registry, factories, permissions = self._runtime_components(bundle)
            verified = (
                bundle
                if bundle.manifest.bundle_format == "agentkit.bundle/v1"
                else PluginBundleResolver(registry).resolve(bundle_root)
            )
            services: dict[str, Any] = {
                "session_service": self._session_service,
                # Providers resolve credential *references* at activation time.
                # The DSH discovery host never receives this service.
                "credential_resolver": self._secret_resolver,
            }
            provider_id, _provider_version = _parse_plugin_ref(
                verified.composition.profile.agent_provider.ref
            )
            if provider_id == KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID:
                services["harness_reasoner"] = (
                    self._harness_reasoner or self._bound_harness_reasoner(verified)
                )
            host = PluginHost(
                registry,
                factories,
                allowed_permissions=permissions,
                services=services,
            )
            try:
                await host.apply(verified.composition.profile)
            except BaseException:
                await host.dispose()
                raise
            candidate = _HostEntry(
                agent_id=verified.manifest.agent_id,
                bundle=verified,
                host=host,
            )
            stale = [
                (digest, entry)
                for digest, entry in self._hosts.items()
                if entry.agent_id == candidate.agent_id and digest != key
            ]
            self._hosts[key] = candidate
            for digest, entry in stale:
                self._hosts.pop(digest, None)
                await entry.host.dispose()
            return candidate

    def _resolve_bundle(self, bundle_root: Path) -> ResolvedPluginBundle:
        registered_ids = {
            manifest.metadata.id for manifest in self._provider_manifests.values()
        }
        try:
            manifest, selection = self._legacy_bundles.select_from_bundle(
                bundle_root,
                registered_provider_ids=registered_ids,
            )
        except LegacyBundleCompatibilityError as error:
            code = (
                "AGENT_PROVIDER_NOT_REGISTERED"
                if error.code == "agent_provider_not_registered"
                else "PLUGIN_BUNDLE_INVALID"
            )
            raise StudioError(
                code,
                "Harness Bundle 兼容性校验失败",
                status_code=409,
                details={"reason": error.code},
            ) from error
        if selection is not None and selection.route == "legacy":
            assert selection.manifest is not None
            profile = CompositionProfile.model_validate(
                {
                    "agentProvider": {
                        "ref": (
                            f"plugin://{selection.manifest.metadata.id}"
                            f"@{selection.manifest.metadata.version}"
                        )
                    }
                }
            )
            registry = PluginRegistry(
                [selection.manifest, *builtin_capability_manifests()]
            )
            try:
                resolved = json.loads(
                    (bundle_root / "resolved-agent-spec.json").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise StudioError(
                    "PLUGIN_BUNDLE_INVALID",
                    "Legacy Harness Bundle 缺少 resolved Agent spec",
                    status_code=409,
                    details={"reason": "legacy_resolved_spec_invalid"},
                ) from error
            if not isinstance(resolved, dict):
                raise StudioError(
                    "PLUGIN_BUNDLE_INVALID",
                    "Legacy Harness resolved Agent spec 必须是对象",
                    status_code=409,
                    details={"reason": "legacy_resolved_spec_invalid"},
                )
            return ResolvedPluginBundle(
                root=bundle_root,
                manifest=manifest,
                resolved_agent_spec=resolved,
                composition=registry.resolve(profile),
            )
        profile = self._read_profile(bundle_root)
        manifests = [
            *builtin_agent_provider_manifests(),
            *builtin_capability_manifests(),
        ]
        external = self._external_manifest(profile)
        if external is not None:
            manifests.append(external)
        try:
            return PluginBundleResolver(PluginRegistry(manifests)).resolve(bundle_root)
        except PluginBundleError as error:
            raise StudioError(
                "PLUGIN_BUNDLE_INVALID",
                "PluginHost Bundle 校验失败",
                status_code=409,
                details={"reason": error.code},
            ) from error

    def _runtime_components(
        self,
        bundle: ResolvedPluginBundle,
    ) -> tuple[PluginRegistry, dict[str, Any], frozenset[str]]:
        manifests: list[PluginManifest] = [
            *builtin_agent_provider_manifests(),
            *builtin_capability_manifests(),
        ]
        factories = builtin_capability_factories(
            state_root=self.workspace.resolve(".agentkit/plugin-runtime/state"),
            secret_resolver=self._secret_resolver.resolve,
        )
        provider_id, provider_version = _parse_plugin_ref(
            bundle.composition.profile.agent_provider.ref
        )
        provider_ref = f"plugin://{provider_id}@{provider_version}"
        if bundle.manifest.bundle_format == "agentkit.bundle/v1":
            manifest = legacy_harness_agent_provider_manifest()
            factory = KsADKHarnessProviderFactory(session_service=self._session_service)
        else:
            manifest = self._provider_manifests.get(provider_ref)
            factory = self._provider_factories.get(provider_ref)
        if manifest is None or factory is None:
            raise StudioError(
                "AGENT_PROVIDER_NOT_REGISTERED",
                "AgentProvider 尚未由当前 DSH Profile 完成预检与注册",
                status_code=409,
                details={"provider": provider_ref},
            )
        manifests.append(manifest)
        factories[provider_id] = factory

        builtin_ids = {
            manifest.metadata.id
            for manifest in (
                *builtin_agent_provider_manifests(),
                *builtin_capability_manifests(),
            )
        }
        allowed = {
            permission
            for manifest in manifests
            if manifest.metadata.id in builtin_ids
            for permission in manifest.spec.permissions
        }
        security = bundle.resolved_agent_spec.get("security")
        if isinstance(security, Mapping):
            raw = security.get("allowedPermissions") or security.get("allowed_permissions") or []
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                allowed.update(str(item) for item in raw)
        return PluginRegistry(manifests), factories, frozenset(allowed)

    def _preflight_bundle(self, bundle: ResolvedPluginBundle) -> None:
        """Reject an unusable composition before a Run or Schedule is admitted.

        Provider activation is intentionally lazy, but permission and graph
        admission are deterministic from the immutable Bundle.  Delaying this
        check until ``RuntimeAdapter.start`` lets an accepted Inbox message
        repeatedly create PENDING Runs that can never become runnable.
        """

        registry, factories, permissions = self._runtime_components(bundle)
        host = PluginHost(
            registry,
            factories,
            allowed_permissions=permissions,
        )
        try:
            host.preflight(bundle.composition.profile)
        except PluginHostError as error:
            code = (
                "PLUGIN_PERMISSION_DENIED"
                if error.code == "plugin_permission_denied"
                else "PLUGIN_RUNTIME_PREFLIGHT_FAILED"
            )
            raise StudioError(
                code,
                "Agent 插件组合未通过运行前检查",
                status_code=409,
                details={"reason": error.code},
            ) from error

    def _bound_harness_reasoner(self, bundle: ResolvedPluginBundle) -> HarnessReasoner:
        spec = bundle.resolved_agent_spec
        model = ResolvedModel.model_validate(spec.get("model"))
        security = spec.get("security")
        security = security if isinstance(security, Mapping) else {}
        network = NetworkPolicy.model_validate(security.get("network") or {})
        execution = spec.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        retry = execution.get("retry")
        retry = retry if isinstance(retry, Mapping) else {}
        return _StudioHarnessReasoner(
            self._model_client,
            model=model,
            network_policy=network,
            timeout_seconds=int(
                execution.get("timeoutSeconds") or execution.get("timeout_seconds") or 120
            ),
            max_attempts=int(
                retry.get("maxAttempts") or retry.get("max_attempts") or 2
            ),
            backoff_seconds=float(
                retry.get("backoffSeconds") or retry.get("backoff_seconds") or 1
            ),
        )

    def _external_manifest(self, profile: CompositionProfile) -> PluginManifest | None:
        plugin_id, version = _parse_plugin_ref(profile.agent_provider.ref)
        builtin_ids = {
            manifest.metadata.id for manifest in builtin_agent_provider_manifests()
        }
        if plugin_id in builtin_ids:
            return None
        provider_ref = f"plugin://{plugin_id}@{version}"
        manifest = self._provider_manifests.get(provider_ref)
        if manifest is None:
            raise StudioError(
                "AGENT_PROVIDER_NOT_REGISTERED",
                "AgentProvider 尚未由当前 DSH Profile 完成预检与注册",
                status_code=409,
                details={"provider": provider_ref},
            )
        return manifest

    def _read_profile(self, bundle_root: Path) -> CompositionProfile:
        try:
            return cast(
                CompositionProfile,
                CompositionProfile.model_validate_json(
                    (bundle_root / "composition-profile.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise StudioError(
                "PLUGIN_BUNDLE_INVALID",
                "PluginHost Bundle 缺少有效的 Composition Profile",
                status_code=409,
            ) from error

    def _bundle_root(self, build: BuildRecord) -> Path:
        if not build.artifact_path:
            raise StudioError("BUILD_NOT_READY", "Build 尚未生成制品", status_code=409)
        archive = cast(
            Path,
            self.workspace.resolve(build.artifact_path, must_exist=True),
        )
        bundle_root = archive.parent / "agent-bundle"
        if not bundle_root.is_dir():
            raise StudioError(
                "PLUGIN_BUNDLE_UNAVAILABLE",
                "Build 缺少可运行的 PluginHost Bundle",
                status_code=409,
            )
        return bundle_root

    @staticmethod
    def _select_model(build: BuildRecord, requested: str | None) -> str:
        allowed = [
            str(item) for item in build.runtime_lock.get("models") or [] if str(item)
        ]
        default = str(build.runtime_lock.get("model") or "").strip()
        if default and default not in allowed:
            allowed.insert(0, default)
        selected = str(requested or default).strip()
        if not selected:
            raise StudioError(
                "AGENT_MODEL_REQUIRED", "Build 没有绑定可运行模型", status_code=422
            )
        if allowed and selected not in allowed:
            raise StudioError(
                "MODEL_NOT_BOUND",
                "请求模型未绑定到当前 Agent Build",
                status_code=422,
                details={"model": selected, "allowedModels": allowed},
            )
        return selected

def _parse_plugin_ref(value: str) -> tuple[str, str]:
    plugin_id, version = value.removeprefix("plugin://").rsplit("@", 1)
    return plugin_id, version


def _normalize_result(raw: Any, *, session_id: str) -> StudioPluginTurnResult:
    if isinstance(raw, HarnessTurnResult):
        return StudioPluginTurnResult(
            output_text=raw.output_text,
            session_id=raw.session_id,
            usage=dict(raw.usage),
            metadata=dict(raw.metadata),
            raw=raw,
        )
    if not isinstance(raw, Mapping):
        raise PluginHostError(
            "provider_result_invalid", "AgentProvider result must be an object"
        )
    output_text = str(raw.get("outputText") or raw.get("output_text") or raw.get("output") or "")
    if not output_text:
        raise PluginHostError(
            "provider_result_invalid", "AgentProvider result must contain outputText"
        )
    usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    return StudioPluginTurnResult(
        output_text=output_text,
        session_id=str(raw.get("sessionId") or raw.get("session_id") or session_id),
        usage=dict(usage),
        metadata=dict(metadata),
        raw=raw,
    )


__all__ = ["StudioPluginRuntime", "StudioPluginTurnResult"]
