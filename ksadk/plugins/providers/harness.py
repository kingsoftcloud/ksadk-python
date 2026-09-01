"""KsADK Harness AgentProvider backed by the canonical RuntimeExecutor.

The provider composes locked MCP, Skill, and Context capabilities, then
delegates every turn to ``HarnessRuntimeAdapter`` through
``invoke_runtime_conversation_once``.  RuntimeEvent persistence and Session
history therefore stay on the existing conversation pipeline; this module does
not create another event stream or transcript store.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from ksadk.harness.config import HarnessConfig, McpToolSpec, SandboxPolicy
from ksadk.harness.reasoner import HarnessReasoner, LiteLLMHarnessReasoner
from ksadk.harness.runtime import HarnessRuntimeAdapter
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginExecutionContext, PluginHostError
from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext, RuntimeRegistry, StartRequest
from ksadk.runtime.conversation_execution import invoke_runtime_conversation_once
from ksadk.sessions import create_session_service
from ksadk.sessions.base import BaseSessionService

_HISTORY_ENVELOPE_PREFIX = "agentkit.conversation-history/v1:"


@dataclass(frozen=True)
class HarnessSkillContribution:
    name: str
    instructions: str


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
            raise PluginHostError(
                "harness_input_invalid", "Harness input must be an object"
            )
        user_id = str(value.get("user_id") or value.get("userId") or "").strip()
        if not user_id:
            raise PluginHostError(
                "harness_input_invalid", "Harness input requires user_id"
            )
        raw_messages = value.get("messages")
        if raw_messages is None and value.get("input") is not None:
            raw_messages = [{"role": "user", "content": value.get("input")}]
        if not isinstance(raw_messages, Sequence) or isinstance(
            raw_messages, (str, bytes)
        ):
            raise PluginHostError(
                "harness_input_invalid", "Harness input requires messages"
            )
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
        session_id = str(
            value.get("session_id") or value.get("sessionId") or ""
        ).strip()
        model = str(value.get("model") or "").strip()
        metadata = value.get("request_metadata") or value.get("requestMetadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise PluginHostError(
                "harness_input_invalid", "request_metadata must be an object"
            )
        return cls(
            user_id=user_id,
            session_id=session_id or None,
            messages=tuple(messages),
            model=model or None,
            request_metadata=dict(metadata) if metadata is not None else None,
            invocation_id=str(
                value.get("invocation_id") or value.get("invocationId") or ""
            ).strip()
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
    ) -> Sequence[McpToolSpec]: ...


@runtime_checkable
class HarnessSkillSource(Protocol):
    def harness_skill(
        self, bundle: ResolvedPluginBundle
    ) -> HarnessSkillContribution: ...


@runtime_checkable
class HarnessContextSource(Protocol):
    async def harness_context(
        self, bundle: ResolvedPluginBundle, request: HarnessTurnRequest
    ) -> str: ...


class _ConversationHistoryReasoner:
    """Restore canonical message roles before calling the Harness reasoner.

    HarnessRuntimeAdapter currently builds ``system + current user`` itself.
    The provider encodes the canonical conversation input into that user slot;
    this wrapper expands it back into structured roles without reimplementing
    the Harness reasoning/tool loop.
    """

    def __init__(self, delegate: HarnessReasoner) -> None:
        self._delegate = delegate

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
    ) -> Any:
        expanded = list(messages)
        if len(expanded) >= 2:
            content = expanded[1].get("content")
            if isinstance(content, str) and content.startswith(_HISTORY_ENVELOPE_PREFIX):
                raw = content.removeprefix(_HISTORY_ENVELOPE_PREFIX)
                try:
                    history = json.loads(raw)
                except json.JSONDecodeError as error:
                    raise RuntimeError("invalid canonical Harness history envelope") from error
                if not isinstance(history, list) or not all(
                    isinstance(item, dict) for item in history
                ):
                    raise RuntimeError("invalid canonical Harness history payload")
                expanded = [expanded[0], *_chat_history(history), *expanded[2:]]
        return await self._delegate.complete(
            model=model,
            prompt=prompt,
            messages=expanded,
            tools=tools,
        )


class _ConversationHistoryHarnessAdapter(HarnessRuntimeAdapter):
    """Compatibility adapter with SessionService as the only history owner."""

    async def start(self, request: StartRequest):  # type: ignore[no-untyped-def]
        preprocessing = request.conversation_preprocessing()
        if preprocessing is not None and preprocessing.messages:
            request = request.model_copy(
                update={
                    "input": _HISTORY_ENVELOPE_PREFIX
                    + json.dumps(
                        preprocessing.messages,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
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
    ) -> None:
        self._plugin_id = plugin_id
        self._session_service = session_service
        self._reasoner = reasoner
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
            raise PluginHostError(
                "harness_provider_unavailable", "Harness provider is not ready"
            )

        mcp_specs: list[McpToolSpec] = []
        mcp_owners: list[str] = []
        for binding in capabilities.all("mcp.connector/v1"):
            if not isinstance(binding.runtime, HarnessMCPSource):
                raise PluginHostError(
                    "harness_mcp_incompatible",
                    f"plugin {binding.plugin_id} cannot project Harness MCP config",
                )
            mcp_specs.extend(binding.runtime.harness_mcp_specs(bundle))
            mcp_owners.append(binding.plugin_id)

        skills: list[HarnessSkillContribution] = []
        skill_owners: list[str] = []
        for binding in capabilities.all("skill.source/v1"):
            if not isinstance(binding.runtime, HarnessSkillSource):
                raise PluginHostError(
                    "harness_skill_incompatible",
                    f"plugin {binding.plugin_id} cannot project Harness instructions",
                )
            contribution = binding.runtime.harness_skill(bundle)
            if not contribution.name.strip() or not contribution.instructions.strip():
                raise PluginHostError(
                    "harness_skill_invalid",
                    f"plugin {binding.plugin_id} returned an empty Skill contribution",
                )
            skills.append(contribution)
            skill_owners.append(binding.plugin_id)

        context_sources: list[HarnessContextSource] = []
        context_owners: list[str] = []
        for binding in capabilities.all("context.contributor/v1"):
            if not isinstance(binding.runtime, HarnessContextSource):
                raise PluginHostError(
                    "harness_context_incompatible",
                    f"plugin {binding.plugin_id} cannot contribute Harness context",
                )
            context_sources.append(binding.runtime)
            context_owners.append(binding.plugin_id)

        execution = bundle.resolved_agent_spec.get("execution")
        strategy = (
            str(execution.get("strategy") or "direct").strip()
            if isinstance(execution, Mapping)
            else "direct"
        )
        if strategy != "direct":
            raise PluginHostError(
                "harness_execution_strategy_unsupported",
                f"KsADK Harness Provider does not support {strategy!r}",
            )

        model, prompt = _bundle_model_and_prompt(bundle)
        if skills:
            prompt = _append_prompt_sections(
                prompt,
                [f"Skill {item.name}:\n{item.instructions}" for item in skills],
            )
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
        self._last_inventory = inventory
        workspace_root = bundle.root / "runtime"
        if not workspace_root.is_dir():
            workspace_root = bundle.root
        return KsADKHarnessActivation(
            bundle=bundle,
            config=config,
            agent_name=bundle.manifest.agent_id,
            workspace_root=workspace_root,
            reasoner=self._reasoner,
            context_sources=tuple(context_sources),
            session_service=self._session_service,
            inventory=inventory,
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
            reasoner = LiteLLMHarnessReasoner()
        self.runtime = KsADKHarnessProviderRuntime(
            plugin_id=manifest.metadata.id,
            session_service=service,
            reasoner=reasoner,
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
        context_sources: tuple[HarnessContextSource, ...],
        session_service: BaseSessionService,
        inventory: HarnessProviderInventory,
    ) -> None:
        self._bundle = bundle
        self._config = config
        self._agent_name = agent_name
        self._workspace_root = workspace_root
        self._reasoner = reasoner
        self._context_sources = context_sources
        self._session_service = session_service
        self._inventory = inventory
        self._ready = False
        self._disposed = False
        self._executors: list[RuntimeExecutor] = []
        self._kernel_adapter: HarnessRuntimeAdapter | None = None

    async def start(self) -> None:
        if self._disposed:
            raise RuntimeError("Harness activation is disposed")
        self._ready = True

    async def health(self) -> bool:
        return self._ready and not self._disposed

    async def execute(self, request: Any) -> HarnessTurnResult:
        if not self._ready or self._disposed:
            raise PluginHostError(
                "harness_activation_unavailable", "Harness activation is not ready"
            )
        turn = HarnessTurnRequest.parse(request)
        context_sections = [
            text.strip()
            for source in self._context_sources
            if (text := await source.harness_context(self._bundle, turn)).strip()
        ]
        config = replace(
            self._config,
            prompt=_append_prompt_sections(self._config.prompt, context_sections),
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

    def runtime_adapter(self) -> HarnessRuntimeAdapter:
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
        if self._kernel_adapter is None:
            self._kernel_adapter = HarnessRuntimeAdapter(
                self._config,
                agent_name=self._agent_name,
                reasoner=self._reasoner,
                workspace_root=self._workspace_root,
            )
        return self._kernel_adapter

    async def drain(self) -> None:
        self._ready = False

    async def dispose(self) -> None:
        self._ready = False
        first_error: BaseException | None = None
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


def _build_direct_backend(
    config: HarnessConfig,
    *,
    agent_name: str,
    workspace_root: Path,
    reasoner: HarnessReasoner,
) -> tuple[RuntimeExecutor, RuntimeLaunchContext]:
    """Internal execution seam; Phase 2 does not expose strategy as a plugin."""

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
        if isinstance(content, Sequence) and not isinstance(
            content, (str, bytes, bytearray)
        ):
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
    "HarnessMCPSource",
    "HarnessProviderInventory",
    "HarnessSkillContribution",
    "HarnessSkillSource",
    "HarnessTurnRequest",
    "HarnessTurnResult",
    "KsADKHarnessProviderFactory",
    "KsADKHarnessProviderRuntime",
]
