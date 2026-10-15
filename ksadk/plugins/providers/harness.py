"""KsADK Harness AgentProvider backed by the canonical RuntimeExecutor.

The provider composes locked MCP, Skill, and Context capabilities, then
delegates every turn to ``HarnessRuntimeAdapter`` through
``invoke_runtime_conversation_once``.  RuntimeEvent persistence and Session
history therefore stay on the existing conversation pipeline; this module does
not create another event stream or transcript store.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from ksadk.harness.config import HarnessConfig, McpToolSpec, SandboxPolicy
from ksadk.harness.reasoner import HarnessReasoner, ModelGatewayHarnessReasoner
from ksadk.harness.runtime import HarnessRuntimeAdapter
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginExecutionContext, PluginHostError
from ksadk.plugins.memory_provider import MemoryProviderBinding
from ksadk.plugins.providers.harness_context import HarnessContextContributorSource
from ksadk.plugins.providers.harness_hooks import HarnessHookBinding, project_harness_hooks
from ksadk.plugins.providers.mcp_projection import (
    MCPResourcePin,
    MCPRuntimeCredential,
    project_mcp_capabilities,
)
from ksadk.runtime import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    RuntimeAdapter,
    RuntimeExecutor,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.conversation_execution import invoke_runtime_conversation_once
from ksadk.sessions import create_session_service
from ksadk.sessions.base import BaseSessionService

_HISTORY_ENVELOPE_PREFIX = "agentkit.conversation-history/v1:"


@dataclass(frozen=True)
class HarnessSkillContribution:
    name: str
    instructions: str
    resource_root: Path | None = None
    # Built-in Bundle Skills carry their locked resource identity into Native.
    # External sources may omit these until they offer an equivalent binding.
    source_ref: str | None = None
    content_digest: str | None = None


@dataclass(frozen=True)
class HarnessProviderInventory:
    provider: str
    execution_strategy: str
    model: str
    history_owner: str
    mcp_servers: tuple[str, ...]
    skills: tuple[str, ...]
    context_contributors: tuple[str, ...]


@dataclass(frozen=True)
class HarnessEngineActivationContext:
    """Locked DSH inputs presented to a host-owned Native adapter builder.

    The Bundle is already resolved by PluginHost; the builder must still bind
    its own authenticated scope, model, tools and verifier for this activation.
    Selection is never inferred from bundle text or a model response.
    """

    bundle: ResolvedPluginBundle
    config: HarnessConfig
    agent_name: str
    workspace_root: Path
    user_workspace_root: Path | None
    skills: tuple[HarnessSkillContribution, ...]
    profile_digest: str
    plugin_lock_digest: str
    activation_key: str
    activation_claims: tuple[tuple[str, str], ...] = ()
    activation_generation: str = ""
    activation_available: Callable[[], bool] | None = None
    mcp_available: Callable[[], Awaitable[bool]] | None = None
    mcp_resource_pins: tuple[MCPResourcePin, ...] = ()
    mcp_runtime_credentials: tuple[MCPRuntimeCredential, ...] = ()
    context_sources: tuple[HarnessContextBinding, ...] = ()
    hook_sources: tuple[HarnessHookBinding, ...] = ()
    memory_sources: tuple[MemoryProviderBinding, ...] = ()


HarnessEngineSelector = Callable[
    [HarnessEngineActivationContext],
    Literal["legacy", "native"] | Awaitable[Literal["legacy", "native"]],
]
HarnessNativeAdapterBuilder = Callable[
    [HarnessEngineActivationContext], RuntimeAdapter | Awaitable[RuntimeAdapter]
]


@dataclass(frozen=True)
class HarnessTurnRequest:
    user_id: str
    session_id: str | None
    messages: tuple[Mapping[str, Any], ...]
    model: str | None = None
    request_metadata: Mapping[str, Any] | None = None
    invocation_id: str | None = None

    @classmethod
    def parse(cls, value: Any) -> "HarnessTurnRequest":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise PluginHostError("harness_input_invalid", "Harness input must be an object")
        user_id = str(value.get("user_id") or value.get("userId") or "").strip()
        if not user_id:
            raise PluginHostError("harness_input_invalid", "Harness input requires user_id")
        raw_messages = value.get("messages")
        if raw_messages is None and value.get("input") is not None:
            raw_messages = [{"role": "user", "content": value.get("input")}]
        if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
            raise PluginHostError("harness_input_invalid", "Harness input requires messages")
        messages: list[Mapping[str, Any]] = []
        for index, message in enumerate(raw_messages):
            if not isinstance(message, Mapping):
                raise PluginHostError(
                    "harness_input_invalid",
                    f"Harness messages[{index}] must be an object",
                )
            role = str(message.get("role") or "").strip()
            if role not in {"system", "user", "assistant", "tool"}:
                raise PluginHostError(
                    "harness_input_invalid",
                    f"Harness messages[{index}] has unsupported role {role!r}",
                )
            messages.append(dict(message))
        if not messages or messages[-1].get("role") != "user":
            raise PluginHostError(
                "harness_input_invalid", "Harness turn must end with a user message"
            )
        session_id = str(value.get("session_id") or value.get("sessionId") or "").strip()
        model = str(value.get("model") or "").strip()
        metadata = value.get("request_metadata") or value.get("requestMetadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise PluginHostError("harness_input_invalid", "request_metadata must be an object")
        return cls(
            user_id=user_id,
            session_id=session_id or None,
            messages=tuple(messages),
            model=model or None,
            request_metadata=dict(metadata) if metadata is not None else None,
            invocation_id=str(value.get("invocation_id") or value.get("invocationId") or "").strip()
            or None,
        )


@dataclass(frozen=True)
class HarnessTurnResult:
    session_id: str
    output_text: str
    usage: Mapping[str, Any]
    metadata: Mapping[str, Any]
    inventory: HarnessProviderInventory


@runtime_checkable
class HarnessMCPSource(Protocol):
    def harness_mcp_specs(
        self, bundle: ResolvedPluginBundle
    ) -> Sequence[McpToolSpec] | Awaitable[Sequence[McpToolSpec]]: ...


@runtime_checkable
class HarnessMCPActivationSource(Protocol):
    async def release_harness_mcp_specs(self, specs: Sequence[McpToolSpec]) -> None: ...


@runtime_checkable
class HarnessSkillSource(Protocol):
    def harness_skill(self, bundle: ResolvedPluginBundle) -> HarnessSkillContribution: ...


@runtime_checkable
class HarnessSkillSetSource(Protocol):
    def harness_skills(
        self, bundle: ResolvedPluginBundle
    ) -> Sequence[HarnessSkillContribution]: ...


@runtime_checkable
class HarnessContextSource(Protocol):
    async def harness_context(
        self, bundle: ResolvedPluginBundle, request: HarnessTurnRequest
    ) -> str: ...


@dataclass(frozen=True)
class HarnessContextBinding:
    """Host-selected source identity; text returned by it grants no authority."""

    plugin_id: str
    plugin_version: str
    runtime: HarnessContextSource | HarnessContextContributorSource
    # Preserve positional SDK callers. A missing slot is admissible only when
    # the locked graph has exactly one Context offer for this owner.
    slot: str | None = None
    contributor: bool = False


class _ConversationHistoryReasoner:
    """Restore canonical message roles before calling the Harness reasoner.

    HarnessRuntimeAdapter currently builds ``system + current user`` itself.
    The provider encodes the canonical conversation input into that user slot;
    this wrapper expands it back into structured roles without reimplementing
    the Harness reasoning/tool loop.
    """

    def __init__(self, delegate: HarnessReasoner) -> None:
        self._delegate = delegate
        # graph_builder 以 engine._reasoner._streaming 决定是否走增量推理；
        # 包装器必须透传该声明，否则 managed Harness 静默回退到整段
        # complete()，前端看不到任何 TEXT_DELTA。
        self._streaming = getattr(delegate, "_streaming", None) is True

    @staticmethod
    def _expand_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Restore the canonical history envelope for either reasoner path.

        The wrapper used to expand only ``complete``.  That made the managed
        Harness graph silently fall back to a buffered model call whenever
        Studio enabled streaming, because the wrapper no longer exposed the
        delegate's ``stream_complete`` method.
        """
        expanded = list(messages)
        if len(expanded) < 2:
            return expanded
        content = expanded[1].get("content")
        if not isinstance(content, str) or not content.startswith(_HISTORY_ENVELOPE_PREFIX):
            return expanded
        raw = content.removeprefix(_HISTORY_ENVELOPE_PREFIX)
        try:
            history = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("invalid canonical Harness history envelope") from error
        if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
            raise RuntimeError("invalid canonical Harness history payload")
        return [expanded[0], *_chat_history(history), *expanded[2:]]

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        expanded = self._expand_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "messages": expanded,
            "tools": tools,
        }
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        return await self._delegate.complete(**kwargs)

    async def stream_complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ):
        """Forward incremental model events while preserving canonical history."""
        stream = getattr(self._delegate, "stream_complete", None)
        if stream is None:
            # Keep compatibility with custom reasoners that only implement
            # ``complete``; callers still receive one terminal turn.
            turn = await self.complete(
                model=model,
                prompt=prompt,
                messages=messages,
                tools=tools,
                **(
                    {"max_output_tokens": max_output_tokens}
                    if max_output_tokens is not None
                    else {}
                ),
                **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
            )
            yield {"turn": turn}
            return
        kwargs = {
            "model": model,
            "prompt": prompt,
            "messages": self._expand_messages(messages),
            "tools": tools,
        }
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        async for item in stream(**kwargs):
            yield item


class _ConversationHistoryHarnessAdapter(HarnessRuntimeAdapter):
    """Compatibility adapter with SessionService as the only history owner."""

    async def start(self, request: StartRequest):  # type: ignore[no-untyped-def]
        preprocessing = request.conversation_preprocessing()
        if preprocessing is not None and preprocessing.messages:
            metadata = {
                key: value
                for key, value in request.metadata.items()
                if key != CONVERSATION_PREPROCESSING_METADATA_KEY
            }
            request = request.model_copy(
                update={
                    "input": _HISTORY_ENVELOPE_PREFIX
                    + json.dumps(
                        preprocessing.messages,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "metadata": metadata,
                }
            )
        return await super().start(request)

    async def execute_request(self, request: StartRequest) -> dict[str, Any]:
        # The standalone Harness adapter intentionally offers process-local
        # continuity.  PluginHost already prepares the complete canonical
        # SessionService history, so retaining that same turn again inside the
        # adapter would create two history owners and duplicate future turns.
        session = self._session_for(request)
        async with session.lock:
            session.messages.clear()
            try:
                result = await self._execute_session_request(request, session)
                return cast(dict[str, Any], result)
            finally:
                session.messages.clear()


class KsADKHarnessProviderRuntime:
    """PluginHost AgentProvider that owns Harness activation assembly."""

    def __init__(
        self,
        *,
        plugin_id: str,
        session_service: BaseSessionService,
        reasoner: HarnessReasoner,
        state_dir: str | None = None,
        checkpoint_dsn: str | None = None,
        execution_policy_resolver: Any | None = None,
        user_workspace_root: Path | None = None,
        engine_selector: HarnessEngineSelector | None = None,
        native_adapter_builder: HarnessNativeAdapterBuilder | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._session_service = session_service
        self._reasoner = reasoner
        self._state_dir = state_dir
        self._checkpoint_dsn = checkpoint_dsn
        self._execution_policy_resolver = execution_policy_resolver
        self._user_workspace_root = user_workspace_root
        self._engine_selector = engine_selector
        self._native_adapter_builder = native_adapter_builder
        self._ready = False
        self._disposed = False
        self._last_inventory: HarnessProviderInventory | None = None

    @property
    def last_inventory(self) -> HarnessProviderInventory | None:
        return self._last_inventory

    @property
    def disposed(self) -> bool:
        return self._disposed

    async def start(self) -> None:
        if self._disposed:
            raise RuntimeError("Harness provider is disposed")
        self._ready = True

    async def health(self) -> bool:
        return self._ready and not self._disposed

    async def drain(self) -> None:
        self._ready = False

    async def dispose(self) -> None:
        self._ready = False
        self._disposed = True

    async def prepare(
        self,
        bundle: ResolvedPluginBundle,
        *,
        capabilities: PluginExecutionContext,
    ) -> "KsADKHarnessActivation":
        if not self._ready or self._disposed:
            raise PluginHostError("harness_provider_unavailable", "Harness provider is not ready")

        async with AsyncExitStack() as mcp_cleanup:
            hook_bindings = project_harness_hooks(bundle, capabilities)
            projection = await project_mcp_capabilities(capabilities, bundle)
            mcp_cleanup.push_async_callback(projection.aclose)
            mcp_specs = list(projection.specs)
            mcp_owners = list(projection.owners)

            skills: list[HarnessSkillContribution] = []
            skill_owners: list[str] = []
            for binding in capabilities.all("skill.source/v1"):
                plural = isinstance(binding.runtime, HarnessSkillSetSource)
                if not plural and not isinstance(binding.runtime, HarnessSkillSource):
                    raise PluginHostError(
                        "harness_skill_incompatible",
                        f"plugin {binding.plugin_id} cannot project Harness instructions",
                    )
                contributions = (
                    tuple(binding.runtime.harness_skills(bundle))
                    if plural
                    else (binding.runtime.harness_skill(bundle),)
                )
                if not contributions:
                    raise PluginHostError(
                        "harness_skill_invalid",
                        f"plugin {binding.plugin_id} returned no Skill contributions",
                    )
                for contribution in contributions:
                    if (
                        not isinstance(contribution, HarnessSkillContribution)
                        or not contribution.name.strip()
                        or not contribution.instructions.strip()
                    ):
                        raise PluginHostError(
                            "harness_skill_invalid",
                            f"plugin {binding.plugin_id} returned an invalid Skill contribution",
                        )
                    skills.append(contribution)
                    skill_owners.append(binding.plugin_id)

            context_sources: list[HarnessContextSource] = []
            context_owners: list[str] = []
            context_bindings: list[HarnessContextBinding] = []
            for binding in capabilities.all("context.contributor/v1"):
                declarations = [item for item in bundle.composition.profile.capabilities
                                if item.ref == f"plugin://{binding.plugin_id}@{binding.plugin_version}"]
                richer = bool(len(declarations) == 1 and declarations[0].config.get(
                    "schema_version",
                ) == "ksadk.harness-context-contributor-binding/v1")
                required_protocol = (HarnessContextContributorSource if richer
                                     else HarnessContextSource)
                if not isinstance(binding.runtime, required_protocol):
                    raise PluginHostError(
                        "harness_context_incompatible",
                        f"plugin {binding.plugin_id} cannot contribute Harness context",
                    )
                if not richer:
                    context_sources.append(binding.runtime)
                context_owners.append(binding.plugin_id)
                context_bindings.append(HarnessContextBinding(
                    binding.plugin_id, binding.plugin_version, binding.runtime,
                    slot=binding.slot,
                    contributor=richer,
                ))

            execution = bundle.resolved_agent_spec.get("execution")
            strategy = (
                str(execution.get("strategy") or "direct").strip()
                if isinstance(execution, Mapping)
                else "direct"
            )
            if strategy not in {"direct", "plan-act-observe"}:
                raise PluginHostError(
                    "harness_execution_strategy_unsupported",
                    f"KsADK Harness Provider does not support {strategy!r}",
                )

            model, prompt = _bundle_model_and_prompt(bundle)
            config = HarnessConfig(
                model=model,
                prompt=prompt,
                mcp_tools=tuple(mcp_specs),
                sandbox=SandboxPolicy(read_only=True),
                runtime="yaml",
            )
            inventory = HarnessProviderInventory(
                provider=self._plugin_id,
                execution_strategy=strategy,
                model=model,
                history_owner="canonical_session_service",
                mcp_servers=tuple(mcp_owners),
                skills=tuple(item.name for item in skills),
                context_contributors=tuple(context_owners),
            )
            workspace_root = bundle.root / "runtime"
            if not workspace_root.is_dir():
                workspace_root = bundle.root
            engine_context = HarnessEngineActivationContext(
                bundle=bundle,
                config=config,
                agent_name=bundle.manifest.agent_id,
                workspace_root=workspace_root,
                user_workspace_root=self._user_workspace_root,
                skills=tuple(skills),
                profile_digest=capabilities.profile_digest,
                plugin_lock_digest=capabilities.plugin_lock_digest,
                activation_key=capabilities.activation_key,
                activation_claims=capabilities.activation_claims,
                activation_generation=capabilities.activation_generation,
                activation_available=capabilities.activation_available,
                mcp_available=projection.available,
                mcp_resource_pins=projection.resource_pins,
                context_sources=tuple(context_bindings),
                hook_sources=hook_bindings,
                memory_sources=tuple(
                    MemoryProviderBinding(binding.plugin_id, binding.plugin_version,
                                          binding.slot, binding.runtime)
                    for binding in capabilities.all("memory.provider/v1")
                ),
            )
            engine_kind: Literal["legacy", "native"] = "legacy"
            if self._engine_selector is not None:
                selected = self._engine_selector(engine_context)
                if inspect.isawaitable(selected):
                    selected = await selected
                if selected not in {"legacy", "native"}:
                    raise PluginHostError(
                        "harness_engine_selection_invalid",
                        "Harness engine selection must be legacy or native",
                    )
                engine_kind = selected
            if hook_bindings and engine_kind != "native":
                raise PluginHostError(
                    "harness_hooks_native_required",
                    "selected harness.hook/v1 capabilities require Native execution",
                )
            if any(binding.contributor for binding in context_bindings) and engine_kind != "native":
                raise PluginHostError(
                    "harness_context_native_required",
                    "typed Context Contributors require the selected Native execution path",
                )
            if engine_kind == "native":
                inventory = replace(inventory, history_owner="native_run_ledger")
                # Only clients consuming the ephemeral callbacks may use the
                # renewable probe. Legacy/static clients retain token expiry.
                engine_context = replace(
                    engine_context,
                    mcp_available=projection.runtime_available,
                    mcp_runtime_credentials=projection.runtime_credentials,
                )
            self._last_inventory = inventory
            return KsADKHarnessActivation(
                bundle=bundle,
                config=config,
                agent_name=bundle.manifest.agent_id,
                workspace_root=workspace_root,
                reasoner=self._reasoner,
                skills=tuple(skills),
                context_sources=tuple(context_sources),
                session_service=self._session_service,
                inventory=inventory,
                mcp_cleanup=mcp_cleanup.pop_all(),
                state_dir=self._state_dir,
                checkpoint_dsn=self._checkpoint_dsn,
                execution_policy_resolver=self._execution_policy_resolver,
                user_workspace_root=self._user_workspace_root,
                engine_context=engine_context,
                engine_kind=engine_kind,
                native_adapter_builder=self._native_adapter_builder,
            )


class KsADKHarnessProviderFactory:
    def __init__(
        self,
        *,
        session_service: BaseSessionService | None = None,
        reasoner: HarnessReasoner | None = None,
    ) -> None:
        self._session_service = session_service
        self._reasoner = reasoner
        self.runtime: KsADKHarnessProviderRuntime | None = None

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> KsADKHarnessProviderRuntime:
        del profile
        service = self._session_service or services.get("session_service")
        if service is None:
            service = create_session_service(backend="memory")
        if not isinstance(service, BaseSessionService):
            raise PluginHostError(
                "harness_session_service_invalid",
                "Harness provider requires a BaseSessionService",
            )
        reasoner = self._reasoner or services.get("harness_reasoner")
        if reasoner is None:
            reasoner = ModelGatewayHarnessReasoner()
        state_dir = services.get("harness_state_dir")
        checkpoint_dsn = services.get("checkpoint_dsn")
        user_workspace_root = services.get("harness_workspace_root")
        engine_selector = services.get("harness_engine_selector")
        native_adapter_builder = services.get("harness_native_adapter_builder")
        if (engine_selector is None) != (native_adapter_builder is None) or (
            engine_selector is not None
            and (not callable(engine_selector) or not callable(native_adapter_builder))
        ):
            raise PluginHostError(
                "harness_native_binding_invalid",
                "Native Harness selection requires a host selector and adapter builder",
            )
        self.runtime = KsADKHarnessProviderRuntime(
            plugin_id=manifest.metadata.id,
            session_service=service,
            reasoner=reasoner,
            state_dir=str(state_dir) if state_dir else None,
            checkpoint_dsn=str(checkpoint_dsn) if checkpoint_dsn else None,
            execution_policy_resolver=services.get("execution_policy_resolver"),
            user_workspace_root=(Path(user_workspace_root) if user_workspace_root else None),
            engine_selector=engine_selector,
            native_adapter_builder=native_adapter_builder,
        )
        return self.runtime


class KsADKHarnessActivation:
    def __init__(
        self,
        *,
        bundle: ResolvedPluginBundle,
        config: HarnessConfig,
        agent_name: str,
        workspace_root: Path,
        reasoner: HarnessReasoner,
        skills: tuple[HarnessSkillContribution, ...],
        context_sources: tuple[HarnessContextSource, ...],
        session_service: BaseSessionService,
        inventory: HarnessProviderInventory,
        state_dir: str | None = None,
        checkpoint_dsn: str | None = None,
        mcp_cleanup: AsyncExitStack | None = None,
        execution_policy_resolver: Any | None = None,
        user_workspace_root: Path | None = None,
        engine_context: HarnessEngineActivationContext | None = None,
        engine_kind: Literal["legacy", "native"] = "legacy",
        native_adapter_builder: HarnessNativeAdapterBuilder | None = None,
    ) -> None:
        self._bundle = bundle
        self._config = config
        self._agent_name = agent_name
        self._workspace_root = workspace_root
        # 用户当前选择的 Studio 工作区（bundle 之外）；文件工具落盘目标。
        self._user_workspace_root = user_workspace_root
        self._engine_context = engine_context
        self._engine_kind = engine_kind
        self._native_adapter_builder = native_adapter_builder
        self._reasoner = reasoner
        self._skills = skills
        self._context_sources = context_sources
        self._session_service = session_service
        self._inventory = inventory
        self._state_dir = state_dir
        self._checkpoint_dsn = checkpoint_dsn
        self._checkpoint_stack: Any | None = None
        self._execution_policy_resolver = execution_policy_resolver
        self._mcp_cleanup = mcp_cleanup
        self._mcp_cleanup_lock = asyncio.Lock()
        self._ready = False
        self._disposed = False
        self._executors: list[RuntimeExecutor] = []
        self._kernel_adapter: RuntimeAdapter | None = None
        self._warmup_task: asyncio.Task[Any] | None = None
        self._warmup_error: BaseException | None = None

    async def start(self) -> None:
        if self._disposed:
            raise RuntimeError("Harness activation is disposed")
        self._ready = True
        # Compilation and MCP/tool discovery are independent of the first
        # user turn.  Start them in the background so Studio can finish
        # opening immediately; the first turn awaits the same task if needed.
        self._warmup_task = asyncio.create_task(self._warmup_kernel())

    async def _warmup_kernel(self) -> None:
        try:
            await self.runtime_adapter()
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # retain the error for the first turn
            self._warmup_error = error

    async def health(self) -> bool:
        return self._ready and not self._disposed

    async def execute(self, request: Any) -> HarnessTurnResult:
        if not self._ready or self._disposed:
            raise PluginHostError(
                "harness_activation_unavailable", "Harness activation is not ready"
            )
        if self._engine_kind == "native":
            # The direct provider path owns separate Session/transcript state.
            # Until it has a Native projection, only Scheduler's runtime_adapter
            # path may execute this pinned Native activation.
            raise PluginHostError(
                "harness_native_direct_path_unavailable",
                "Native Harness activation requires the Scheduler runtime adapter path",
            )
        turn = HarnessTurnRequest.parse(request)
        context_sections = [
            text.strip()
            for source in self._context_sources
            if (text := await source.harness_context(self._bundle, turn)).strip()
        ]
        config = replace(
            self._config,
            prompt=_append_prompt_sections(
                self._config.prompt,
                [
                    *[f"Skill {item.name}:\n{item.instructions}" for item in self._skills],
                    *context_sections,
                ],
            ),
        )
        executor, launch_context = _build_direct_backend(
            config,
            agent_name=self._agent_name,
            workspace_root=self._workspace_root,
            reasoner=self._reasoner,
        )
        self._executors.append(executor)
        preparation = await executor.prepare_start(launch_context)
        session_id, result = await invoke_runtime_conversation_once(
            executor=executor,
            launch_context=launch_context,
            agent_id=self._agent_name,
            user_id=turn.user_id,
            messages=[dict(item) for item in turn.messages],
            session_id=turn.session_id,
            model=turn.model or config.model,
            instructions=config.prompt,
            request_metadata=turn.request_metadata,
            invocation_id=turn.invocation_id,
            session_service_provider=lambda: self._session_service,
            runtime_preparation=preparation,
        )
        return HarnessTurnResult(
            session_id=session_id,
            output_text=str(result.get("output_text") or ""),
            usage=dict(result.get("usage") or {}),
            metadata=dict(result.get("metadata") or {}),
            inventory=self._inventory,
        )

    async def runtime_adapter(self) -> RuntimeAdapter:
        """Return the activation-owned adapter used by AgentKernel Scheduler.

        The immutable profile has already assembled model instructions, MCP,
        Skills, sandbox and permissions into ``self._config``.  Reusing one
        adapter per session activation preserves Harness' honest process-local
        continuity while AgentKernel remains the only event persistence owner.
        """

        if not self._ready or self._disposed:
            raise PluginHostError(
                "harness_activation_unavailable", "Harness activation is not ready"
            )
        current = asyncio.current_task()
        if self._warmup_task is not None and self._warmup_task is not current:
            await asyncio.shield(self._warmup_task)
            if self._warmup_error is not None:
                error, self._warmup_error = self._warmup_error, None
                raise error
        if self._kernel_adapter is None:
            if self._engine_kind == "native":
                from ksadk.harness.native_runtime import NativeRuntimeAdapter

                if self._engine_context is None or self._native_adapter_builder is None:
                    raise PluginHostError(
                        "harness_native_binding_unavailable",
                        "Native Harness host bindings are unavailable",
                    )
                adapter = self._native_adapter_builder(self._engine_context)
                if inspect.isawaitable(adapter):
                    adapter = await adapter
                if not isinstance(adapter, NativeRuntimeAdapter):
                    raise PluginHostError(
                        "harness_native_adapter_invalid",
                        "Native Harness host builder did not return a NativeRuntimeAdapter",
                    )
                self._kernel_adapter = adapter
            else:
                from ksadk.plugins.providers.harness_managed import (
                    build_managed_provider_adapter,
                )

                self._kernel_adapter = await build_managed_provider_adapter(
                    self._config,
                    agent_name=self._agent_name,
                    reasoner=self._reasoner,
                    workspace_root=self._workspace_root,
                    user_workspace_root=self._user_workspace_root,
                    skills=self._skills,
                    tool_contracts=dict(self._bundle.resolved_agent_spec),
                    bundle_root=self._bundle.root,
                    state_dir=self._state_dir,
                    checkpoint_dsn=self._checkpoint_dsn,
                    execution_policy_resolver=self._execution_policy_resolver,
                )
                self._checkpoint_stack = getattr(self._kernel_adapter, "_checkpoint_stack", None)
        return self._kernel_adapter

    async def drain(self) -> None:
        self._ready = False

    async def abort(self) -> None:
        """Revoke external MCP credentials before waiting on a stuck turn."""

        await self._release_mcp_credentials()

    async def dispose(self) -> None:
        self._ready = False
        if self._warmup_task is not None and not self._warmup_task.done():
            self._warmup_task.cancel()
            await asyncio.gather(self._warmup_task, return_exceptions=True)
        self._warmup_task = None
        first_error: BaseException | None = None
        if self._checkpoint_stack is not None:
            try:
                await self._checkpoint_stack.aclose()
            except BaseException as error:  # cleanup must continue
                first_error = error
            self._checkpoint_stack = None
        try:
            await self._release_mcp_credentials()
        except BaseException as error:  # cleanup must continue
            first_error = error
        if self._kernel_adapter is not None:
            try:
                await self._kernel_adapter.close_all()
            except BaseException as error:  # cleanup must continue
                first_error = error
            self._kernel_adapter = None
        for executor in reversed(self._executors):
            try:
                await executor.close_all()
            except BaseException as error:  # cleanup must continue
                if first_error is None:
                    first_error = error
        self._executors.clear()
        self._disposed = True
        if first_error is not None:
            raise first_error

    async def _release_mcp_credentials(self) -> None:
        async with self._mcp_cleanup_lock:
            cleanup = self._mcp_cleanup
            if cleanup is not None:
                await cleanup.aclose()
                self._mcp_cleanup = None


def _build_direct_backend(
    config: HarnessConfig,
    *,
    agent_name: str,
    workspace_root: Path,
    reasoner: HarnessReasoner,
) -> tuple[RuntimeExecutor, RuntimeLaunchContext]:
    """Internal execution seam; Release does not expose strategy as a plugin."""

    history_reasoner = _ConversationHistoryReasoner(reasoner)
    registry = RuntimeRegistry()
    registry.register(
        "harness",
        lambda _context: _ConversationHistoryHarnessAdapter(
            config,
            agent_name=agent_name,
            reasoner=history_reasoner,
            workspace_root=workspace_root,
        ),
    )
    return RuntimeExecutor(registry), RuntimeLaunchContext(
        runtime_type="harness",
        project_dir=workspace_root,
        config={
            "model": config.model,
            "base_instructions": config.prompt,
            "sandbox_read_only": config.sandbox.read_only,
        },
    )


def _bundle_model_and_prompt(bundle: ResolvedPluginBundle) -> tuple[str, str]:
    spec = bundle.resolved_agent_spec
    raw_model = spec.get("model")
    model = (
        str(raw_model.get("model") or "").strip()
        if isinstance(raw_model, Mapping)
        else str(raw_model or "").strip()
    )
    instructions = spec.get("instructions")
    if not isinstance(instructions, Mapping):
        instructions = {}
    system = str(instructions.get("system") or "").strip()
    task = str(instructions.get("task") or "").strip()
    prompt = _append_prompt_sections(system, [task])
    if not model:
        raise PluginHostError(
            "harness_bundle_model_missing", "Bundle resolved Agent spec has no model"
        )
    if not prompt:
        raise PluginHostError(
            "harness_bundle_prompt_missing", "Bundle resolved Agent spec has no instructions"
        )
    return model, prompt


def _append_prompt_sections(base: str, sections: Sequence[str]) -> str:
    values = [base.strip(), *(section.strip() for section in sections)]
    return "\n\n".join(value for value in values if value)


def _chat_history(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Translate Responses message content into the Harness chat shape."""

    normalized: list[dict[str, Any]] = []
    for item in history:
        role = str(item.get("role") or "").strip().lower()
        if role == "model":
            role = "assistant"
        if role not in {"user", "assistant", "tool"}:
            continue
        content = item.get("content")
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            segments = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, Mapping) and part.get("text") is not None
            ]
            content = "\n".join(segment for segment in segments if segment)
        message = {"role": role, "content": str(content or "")}
        for key in ("tool_call_id", "name", "tool_calls"):
            if key in item:
                message[key] = item[key]
        normalized.append(message)
    return normalized


__all__ = [
    "HarnessContextSource",
    "HarnessContextBinding",
    "HarnessEngineActivationContext",
    "HarnessEngineSelector",
    "HarnessMCPSource",
    "HarnessMCPActivationSource",
    "HarnessNativeAdapterBuilder",
    "HarnessProviderInventory",
    "HarnessSkillContribution",
    "HarnessSkillSource",
    "HarnessTurnRequest",
    "HarnessTurnResult",
    "KsADKHarnessProviderFactory",
    "KsADKHarnessProviderRuntime",
]
