"""Runtime-native Codex AgentProvider for the controlled PluginHost.

The provider only projects an immutable AgentBundle into the existing Codex
RuntimeAdapter/RuntimeExecutor conversation path.  Canonical SessionEvents and
native thread continuation remain owned by that path; no provider-local event
stream or transcript is created here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginExecutionContext, PluginHostError
from ksadk.runtime import (
    RuntimeExecutor,
    RuntimeLaunchContext,
    RuntimeServices,
    build_default_runtime_registry,
)
from ksadk.runtime.conversation_execution import invoke_runtime_conversation_once
from ksadk.sessions import create_session_service
from ksadk.sessions.base import BaseSessionService


@dataclass(frozen=True)
class CodexProviderInventory:
    provider: str
    model: str
    mcp_servers: tuple[str, ...]
    skills: tuple[str, ...]


@dataclass(frozen=True)
class CodexTurnRequest:
    user_id: str
    session_id: str | None
    messages: tuple[Mapping[str, Any], ...]
    request_metadata: Mapping[str, Any] | None = None
    invocation_id: str | None = None
    model: str | None = None
    collaboration_mode: str | None = None
    goal_objective: str | None = None

    @classmethod
    def parse(cls, value: Any) -> "CodexTurnRequest":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise PluginHostError("codex_input_invalid", "Codex input must be an object")
        allowed_fields = {
            "user_id",
            "userId",
            "session_id",
            "sessionId",
            "messages",
            "input",
            "request_metadata",
            "requestMetadata",
            "invocation_id",
            "invocationId",
            "model",
            "collaboration_mode",
            "collaborationMode",
            "goal_objective",
            "goalObjective",
        }
        unsupported = sorted(str(field) for field in value if field not in allowed_fields)
        if unsupported:
            raise PluginHostError(
                "codex_input_unsupported",
                "Codex input contains undeclared fields: " + ", ".join(unsupported),
            )
        _reject_aliased_duplicates(
            value,
            ("user_id", "userId"),
            ("session_id", "sessionId"),
            ("request_metadata", "requestMetadata"),
            ("invocation_id", "invocationId"),
            ("collaboration_mode", "collaborationMode"),
            ("goal_objective", "goalObjective"),
        )
        if "messages" in value and "input" in value:
            raise PluginHostError(
                "codex_input_invalid",
                "Codex input must use exactly one of messages or input",
            )
        user_id = str(value.get("user_id") or value.get("userId") or "").strip()
        if not user_id:
            raise PluginHostError("codex_input_invalid", "Codex input requires user_id")
        raw_messages = value.get("messages")
        if raw_messages is None and value.get("input") is not None:
            raw_messages = ({"role": "user", "content": value.get("input")},)
        if not isinstance(raw_messages, Sequence) or isinstance(
            raw_messages, (str, bytes)
        ):
            raise PluginHostError("codex_input_invalid", "Codex input requires messages")
        messages: list[Mapping[str, Any]] = []
        for index, message in enumerate(raw_messages):
            if not isinstance(message, Mapping):
                raise PluginHostError(
                    "codex_input_invalid", f"Codex messages[{index}] must be an object"
                )
            role = str(message.get("role") or "").strip()
            if role not in {"system", "user", "assistant", "tool"}:
                raise PluginHostError(
                    "codex_input_invalid",
                    f"Codex messages[{index}] has unsupported role {role!r}",
                )
            messages.append(dict(message))
        if not messages or messages[-1].get("role") != "user":
            raise PluginHostError(
                "codex_input_invalid", "Codex turn must end with a user message"
            )
        metadata = value.get("request_metadata") or value.get("requestMetadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise PluginHostError(
                "codex_input_invalid", "request_metadata must be an object"
            )
        session_id = str(
            value.get("session_id") or value.get("sessionId") or ""
        ).strip()
        invocation_id = str(
            value.get("invocation_id") or value.get("invocationId") or ""
        ).strip()
        if len(invocation_id) > 256:
            raise PluginHostError(
                "codex_input_invalid", "Codex invocation_id exceeds 256 characters"
            )
        model = str(value.get("model") or "").strip()
        if len(model) > 256:
            raise PluginHostError(
                "codex_input_invalid", "Codex model exceeds 256 characters"
            )
        collaboration_mode = (
            str(value.get("collaboration_mode") or value.get("collaborationMode") or "")
            .strip()
            .lower()
        )
        if collaboration_mode and collaboration_mode not in {"default", "plan"}:
            raise PluginHostError(
                "codex_input_unsupported",
                "Codex collaboration_mode must be default or plan",
            )
        goal_objective = str(
            value.get("goal_objective") or value.get("goalObjective") or ""
        ).strip()
        if len(goal_objective) > 4096:
            raise PluginHostError(
                "codex_input_invalid", "Codex goal_objective exceeds 4096 characters"
            )
        return cls(
            user_id=user_id,
            session_id=session_id or None,
            messages=tuple(messages),
            request_metadata=dict(metadata) if metadata is not None else None,
            invocation_id=invocation_id or None,
            model=model or None,
            collaboration_mode=collaboration_mode or None,
            goal_objective=goal_objective or None,
        )


@dataclass(frozen=True)
class CodexTurnResult:
    session_id: str
    output_text: str
    usage: Mapping[str, Any]
    metadata: Mapping[str, Any]
    inventory: CodexProviderInventory


@dataclass(frozen=True)
class _CodexBundleConfig:
    model: str
    allowed_models: tuple[str, ...]
    prompt: str
    project_dir: Path
    launch_config: Mapping[str, Any]
    inventory: CodexProviderInventory


class CodexAgentProviderRuntime:
    """ExecutableAgentProvider that preserves Codex native loop ownership."""

    def __init__(
        self,
        *,
        plugin_id: str,
        session_service: BaseSessionService,
        codex_client_factory: Callable[..., Any] | None,
        credential_resolver: Any = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._session_service = session_service
        self._client_factory = codex_client_factory
        self._credentials = credential_resolver
        self._ready = False
        self._disposed = False
        self._last_activation: CodexAgentActivation | None = None

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def last_activation(self) -> "CodexAgentActivation | None":
        return self._last_activation

    async def start(self) -> None:
        if self._disposed:
            raise RuntimeError("Codex provider is disposed")
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
    ) -> "CodexAgentActivation":
        if not self._ready or self._disposed:
            raise PluginHostError(
                "codex_provider_unavailable", "Codex provider is not ready"
            )
        _reject_external_execution(bundle, capabilities)
        config = _resolve_bundle_config(
            bundle,
            plugin_id=self._plugin_id,
            credential_resolver=self._credentials,
        )
        activation = CodexAgentActivation(
            bundle=bundle,
            config=config,
            session_service=self._session_service,
            codex_client_factory=self._client_factory,
        )
        self._last_activation = activation
        return activation


class CodexAgentProviderFactory:
    def __init__(
        self,
        *,
        session_service: BaseSessionService | None = None,
        codex_client_factory: Callable[..., Any] | None = None,
        credential_resolver: Any = None,
    ) -> None:
        self._session_service = session_service
        self._client_factory = codex_client_factory
        self._credentials = credential_resolver
        self.runtime: CodexAgentProviderRuntime | None = None

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> CodexAgentProviderRuntime:
        del profile
        service = self._session_service or services.get("session_service")
        if service is None:
            service = create_session_service(backend="memory")
        if not isinstance(service, BaseSessionService):
            raise PluginHostError(
                "codex_session_service_invalid",
                "Codex provider requires a BaseSessionService",
            )
        client_factory = self._client_factory or services.get("codex_client_factory")
        credentials = self._credentials or services.get("credential_resolver")
        self.runtime = CodexAgentProviderRuntime(
            plugin_id=manifest.metadata.id,
            session_service=service,
            codex_client_factory=client_factory,
            credential_resolver=credentials,
        )
        return self.runtime


class CodexAgentActivation:
    def __init__(
        self,
        *,
        bundle: ResolvedPluginBundle,
        config: _CodexBundleConfig,
        session_service: BaseSessionService,
        codex_client_factory: Callable[..., Any] | None,
    ) -> None:
        self._bundle = bundle
        self._config = config
        self._session_service = session_service
        self._executor = RuntimeExecutor(build_default_runtime_registry())
        self._launch_context = RuntimeLaunchContext(
            runtime_type="codex",
            project_dir=config.project_dir,
            config=config.launch_config,
            services=RuntimeServices(codex_client_factory=codex_client_factory),
        )
        self._ready = False
        self._disposed = False
        self._kernel_adapters: list[Any] = []

    @property
    def disposed(self) -> bool:
        return self._disposed

    async def start(self) -> None:
        if self._disposed:
            raise RuntimeError("Codex activation is disposed")
        self._ready = True

    async def health(self) -> bool:
        return self._ready and not self._disposed

    async def execute(self, request: Any) -> CodexTurnResult:
        if not self._ready or self._disposed:
            raise PluginHostError(
                "codex_activation_unavailable", "Codex activation is not ready"
            )
        turn = CodexTurnRequest.parse(request)
        selected_model = turn.model or self._config.model
        if selected_model not in self._config.allowed_models:
            raise PluginHostError(
                "codex_model_unsupported",
                f"Codex model {selected_model!r} is not declared by the immutable Bundle",
            )
        launch_context = self._turn_launch_context(turn)
        preparation = await self._executor.prepare_start(launch_context)
        session_id, result = await invoke_runtime_conversation_once(
            executor=self._executor,
            launch_context=launch_context,
            agent_id=self._bundle.manifest.agent_id,
            user_id=turn.user_id,
            messages=[dict(item) for item in turn.messages],
            session_id=turn.session_id,
            model=selected_model,
            instructions=self._config.prompt,
            request_metadata=turn.request_metadata,
            invocation_id=turn.invocation_id,
            session_service_provider=lambda: self._session_service,
            runtime_preparation=preparation,
        )
        return CodexTurnResult(
            session_id=session_id,
            output_text=str(result.get("output_text") or ""),
            usage=dict(result.get("usage") or {}),
            metadata=dict(result.get("metadata") or {}),
            inventory=CodexProviderInventory(
                provider=self._config.inventory.provider,
                model=selected_model,
                mcp_servers=self._config.inventory.mcp_servers,
                skills=self._config.inventory.skills,
            ),
        )

    def runtime_adapter(self) -> Any:
        """Create one activation-owned native adapter for AgentKernel.

        Each Kernel command receives a fresh App Server transport. Durable
        SessionEvent metadata reconnects the next command to the same Codex
        Thread, while retaining adapters here lets activation disposal close a
        command that is still active during Profile drain.
        """

        if not self._ready or self._disposed:
            raise PluginHostError(
                "codex_activation_unavailable", "Codex activation is not ready"
            )
        adapter = self._executor.create_adapter(self._launch_context)
        self._kernel_adapters.append(adapter)
        return adapter

    def _turn_launch_context(self, turn: CodexTurnRequest) -> RuntimeLaunchContext:
        config = dict(self._config.launch_config)
        if turn.model:
            config["model"] = turn.model
        if turn.collaboration_mode:
            config["collaboration_mode"] = turn.collaboration_mode
        if turn.goal_objective:
            config["goal_objective"] = turn.goal_objective
        return RuntimeLaunchContext(
            runtime_type=self._launch_context.runtime_type,
            project_dir=self._launch_context.project_dir,
            config=config,
            services=self._launch_context.services,
            deployment_mode=self._launch_context.deployment_mode,
        )

    async def drain(self) -> None:
        self._ready = False

    async def dispose(self) -> None:
        self._ready = False
        first_error: BaseException | None = None
        for adapter in reversed(self._kernel_adapters):
            close_all = getattr(adapter, "close_all", None)
            if not callable(close_all):
                continue
            try:
                await close_all()
            except BaseException as error:  # cleanup must continue
                if first_error is None:
                    first_error = error
        self._kernel_adapters.clear()
        try:
            await self._executor.close_all()
        except BaseException as error:  # cleanup must continue
            if first_error is None:
                first_error = error
        self._disposed = True
        if first_error is not None:
            raise first_error


def _reject_aliased_duplicates(
    value: Mapping[str, Any],
    *aliases: tuple[str, str],
) -> None:
    for snake_case, camel_case in aliases:
        if snake_case in value and camel_case in value:
            raise PluginHostError(
                "codex_input_invalid",
                f"Codex input cannot contain both {snake_case} and {camel_case}",
            )


def _reject_external_execution(
    bundle: ResolvedPluginBundle,
    capabilities: PluginExecutionContext,
) -> None:
    del capabilities
    profile_config = bundle.composition.profile.agent_provider.config
    allowed_config = {"runtimeType", "runtimeVersion"}
    unsupported_config = sorted(set(profile_config) - allowed_config)
    execution = bundle.resolved_agent_spec.get("execution")
    strategy = (
        str(execution.get("strategy") or "direct").strip()
        if isinstance(execution, Mapping)
        else "direct"
    )
    if unsupported_config:
        raise PluginHostError(
            "codex_provider_config_unsupported",
            "Codex AgentProvider received unsupported configuration: "
            + ", ".join(unsupported_config),
        )
    if strategy != "direct":
        raise PluginHostError(
            "codex_external_execution_unsupported",
            f"Codex AgentProvider owns its execution semantics and does not support {strategy!r}",
        )


def _resolve_bundle_config(
    bundle: ResolvedPluginBundle,
    *,
    plugin_id: str,
    credential_resolver: Any,
) -> _CodexBundleConfig:
    spec = bundle.resolved_agent_spec
    raw_model = spec.get("model")
    if not isinstance(raw_model, Mapping):
        raise PluginHostError(
            "codex_bundle_model_invalid", "Bundle resolved model must be an object"
        )
    model = str(raw_model.get("model") or "").strip()
    if not model:
        raise PluginHostError(
            "codex_bundle_model_missing", "Bundle resolved Agent spec has no model"
        )
    allowed_models = _resolve_allowed_models(bundle, default_model=model)
    instructions = spec.get("instructions")
    if not isinstance(instructions, Mapping):
        raise PluginHostError(
            "codex_bundle_prompt_invalid", "Bundle instructions must be an object"
        )
    prompt = "\n\n".join(
        part
        for part in (
            str(instructions.get("system") or "").strip(),
            str(instructions.get("task") or "").strip(),
        )
        if part
    )
    if not prompt:
        raise PluginHostError(
            "codex_bundle_prompt_missing", "Bundle resolved Agent spec has no instructions"
        )
    capabilities = spec.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise PluginHostError(
            "codex_bundle_capabilities_invalid", "Bundle capabilities must be an object"
        )
    if capabilities.get("tools"):
        raise PluginHostError(
            "codex_tools_unsupported", "Codex only accepts its native tools, MCP, and Skills"
        )
    skills = _resolve_skills(bundle.root, capabilities.get("skills"))
    mcp_servers, env = _resolve_mcp(
        capabilities.get("mcpServers") or capabilities.get("mcp_servers"),
        credential_resolver=credential_resolver,
    )
    execution = spec.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    project_dir = bundle.root / "runtime"
    if not project_dir.is_dir():
        project_dir = bundle.root
    launch_config: dict[str, Any] = {
        "model": model,
        "models": list(allowed_models),
        "prompt": str(instructions.get("system") or "").strip(),
        "task_prompt": str(instructions.get("task") or "").strip(),
        "sandbox": str(execution.get("sandbox") or "read_only"),
        "approval_mode": str(execution.get("approvalMode") or execution.get("approval_mode") or ""),
        "turn_timeout_seconds": int(
            execution.get("timeoutSeconds") or execution.get("timeout_seconds") or 120
        ),
        "mcp_servers": mcp_servers,
        "skills": skills,
        "env": env,
    }
    return _CodexBundleConfig(
        model=model,
        allowed_models=allowed_models,
        prompt=prompt,
        project_dir=project_dir,
        launch_config=launch_config,
        inventory=CodexProviderInventory(
            provider=plugin_id,
            model=model,
            mcp_servers=tuple(item["name"] for item in mcp_servers),
            skills=tuple(item["name"] for item in skills),
        ),
    )


def _resolve_allowed_models(
    bundle: ResolvedPluginBundle,
    *,
    default_model: str,
) -> tuple[str, ...]:
    """Read the immutable model allowlist produced by the Bundle compiler.

    Older Bundle fixtures without ``runtime-lock.json`` remain pinned to their
    resolved default.  A declared lock is already digest-verified by
    ``PluginBundleResolver``; malformed model inventory must fail activation
    instead of silently widening run-level model selection.
    """

    if not any(
        entry.path == "runtime-lock.json" for entry in bundle.manifest.files
    ):
        return (default_model,)
    try:
        payload = json.loads(
            (bundle.root / "runtime-lock.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PluginHostError(
            "codex_bundle_model_inventory_invalid",
            "Bundle runtime-lock.json is not a valid model inventory",
        ) from error
    if not isinstance(payload, Mapping):
        raise PluginHostError(
            "codex_bundle_model_inventory_invalid",
            "Bundle runtime-lock.json must be an object",
        )
    raw_models = payload.get("models")
    if not isinstance(raw_models, Sequence) or isinstance(raw_models, (str, bytes)):
        raise PluginHostError(
            "codex_bundle_model_inventory_invalid",
            "Bundle runtime-lock.json must declare models as a list",
        )
    models = tuple(
        dict.fromkeys(str(item).strip() for item in raw_models if str(item).strip())
    )
    if not models or default_model not in models:
        raise PluginHostError(
            "codex_bundle_model_inventory_invalid",
            "Bundle model inventory must include its resolved default model",
        )
    return models


def _resolve_skills(root: Path, value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PluginHostError("codex_skill_invalid", "Bundle Skills must be a list")
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PluginHostError(
                "codex_skill_invalid", f"Bundle Skills[{index}] must be an object"
            )
        name = str(item.get("name") or "").strip()
        relative = PurePosixPath(str(item.get("bundlePath") or item.get("bundle_path") or ""))
        if not name or not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise PluginHostError(
                "codex_skill_invalid", f"Bundle Skills[{index}] has an invalid native path"
            )
        path = (root / Path(*relative.parts)).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise PluginHostError(
                "codex_skill_invalid", f"Bundle Skill {name!r} escapes the Bundle root"
            ) from error
        if not (path / "SKILL.md").is_file():
            raise PluginHostError(
                "codex_skill_missing", f"Bundle Skill {name!r} has no SKILL.md"
            )
        if name in seen:
            raise PluginHostError("codex_skill_invalid", f"duplicate Bundle Skill {name!r}")
        seen.add(name)
        # Codex's native SkillInput expects the concrete SKILL.md source, not
        # its containing directory.  A directory is accepted by the Python
        # model but silently omitted by the real App Server turn projection.
        resolved.append({"name": name, "path": str(path / "SKILL.md")})
    return resolved


def _resolve_mcp(
    value: Any,
    *,
    credential_resolver: Any,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    if value is None:
        return [], {}
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PluginHostError("codex_mcp_invalid", "Bundle MCP servers must be a list")
    servers: list[dict[str, str]] = []
    env: dict[str, str] = {}
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PluginHostError(
                "codex_mcp_invalid", f"Bundle MCP servers[{index}] must be an object"
            )
        name = str(item.get("name") or "").strip()
        transport = str(item.get("transport") or "").strip().lower()
        url = str(item.get("endpointUrl") or item.get("endpoint_url") or "").strip()
        if not name or name in seen:
            raise PluginHostError("codex_mcp_invalid", "Bundle MCP names must be unique")
        if transport not in {"http", "sse"} or not url:
            raise PluginHostError(
                "codex_mcp_transport_unsupported",
                f"Codex Bundle MCP {name!r} requires an http/sse endpoint",
            )
        env_refs = item.get("envRefs") or item.get("env_refs") or {}
        if not isinstance(env_refs, Mapping):
            raise PluginHostError(
                "codex_mcp_invalid", f"Bundle MCP {name!r} envRefs must be an object"
            )
        if len(env_refs) > 1:
            raise PluginHostError(
                "codex_mcp_credentials_unsupported",
                f"Codex HTTP MCP {name!r} accepts at most one bearer credential",
            )
        server = {"name": name, "url": url}
        for env_name, reference in env_refs.items():
            env_key = str(env_name).strip()
            ref = str(reference).strip()
            if not env_key or not ref.startswith(
                ("env://", "secret://", "credential://", "vault://")
            ):
                raise PluginHostError(
                    "codex_mcp_credential_invalid",
                    f"Bundle MCP {name!r} contains an invalid credential reference",
                )
            if credential_resolver is None:
                raise PluginHostError(
                    "codex_mcp_credential_unavailable",
                    f"Bundle MCP {name!r} requires a credential resolver",
                )
            try:
                value = (
                    credential_resolver.resolve(ref)
                    if hasattr(credential_resolver, "resolve")
                    else credential_resolver(ref)
                )
            except Exception as error:
                raise PluginHostError(
                    "codex_mcp_credential_unavailable",
                    f"Bundle MCP {name!r} credential could not be resolved",
                ) from error
            if not str(value):
                raise PluginHostError(
                    "codex_mcp_credential_unavailable",
                    f"Bundle MCP {name!r} credential is empty",
                )
            env[env_key] = str(value)
            server["env_key"] = env_key
        servers.append(server)
        seen.add(name)
    return servers, env


__all__ = [
    "CodexAgentProviderFactory",
    "CodexAgentProviderRuntime",
    "CodexProviderInventory",
    "CodexTurnRequest",
    "CodexTurnResult",
]
