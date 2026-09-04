"""内置 RuntimeAdapter 的唯一默认 Factory Registry。"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ksadk.codex.client import AsyncCodexClient, CodexPluginBootstrap
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import RuntimeAdapter, RuntimeRegistry, StartRequest
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter, LangGraphRuntimeAdapter
from ksadk.runtime.launch import RuntimeLaunchContext


def kernel_start_request_defaults(context: RuntimeLaunchContext) -> dict[str, Any]:
    """Project an admitted launch manifest into immutable Kernel turn defaults.

    The durable Kernel owns enqueue ordering, while the deployment manifest
    owns model, instructions, sandbox and approval policy.  Keeping this
    projection beside the RuntimeAdapter factory prevents the Kernel ingress
    from trusting caller-supplied execution policy.
    """

    config = dict(context.config)
    defaults: dict[str, Any] = {}
    detection_name = str(getattr(context.detection, "name", "") or "").strip()
    if detection_name:
        defaults["agent_id"] = detection_name
    model = str(config.get("model") or "").strip()
    if model:
        defaults["model"] = model
    raw_allowed_models = (
        config.get("models") or config.get("allowed_models") or config.get("allowedModels") or []
    )
    allowed_models = (
        {str(item).strip() for item in raw_allowed_models if str(item).strip()}
        if isinstance(raw_allowed_models, (list, tuple, set))
        else set()
    )
    # 不把默认 model 自动加进白名单:白名单只在显式声明 models/allowedModels
    # 时才存在(显式声明 = 收紧;只配默认 = 不限制 run 级覆盖)。
    if allowed_models:
        defaults["allowed_models"] = sorted(allowed_models)
    if context.runtime_type != "codex":
        return defaults

    prompt = str(config.get("prompt") or "").strip()
    task_prompt = str(config.get("task_prompt") or "").strip()
    base_instructions = prompt
    if task_prompt:
        base_instructions = f"{prompt}\n\n{task_prompt}" if prompt else task_prompt

    raw_sandbox = str(config.get("sandbox") or "read_only").strip().lower()
    raw_approval = str(config.get("approval_mode") or "").strip().lower()
    approval_profiles = {
        "ask": ("workspace-write", "manual"),
        "risk": ("workspace-write", "auto_review"),
        "full": ("full-access", "deny_all"),
    }
    sandbox_profiles = {
        "read_only": ("read-only", "deny_all"),
        "read-only": ("read-only", "deny_all"),
        "workspace_write": ("workspace-write", "deny_all"),
        "workspace-write": ("workspace-write", "deny_all"),
        "workspace_write_auto": ("workspace-write", "auto_review"),
        "workspace-write-auto": ("workspace-write", "auto_review"),
        "full_access": ("full-access", "deny_all"),
        "full-access": ("full-access", "deny_all"),
    }
    if raw_approval in approval_profiles:
        sandbox, approval = approval_profiles[raw_approval]
    else:
        sandbox, default_approval = sandbox_profiles.get(raw_sandbox, ("read-only", "deny_all"))
        approval = raw_approval or default_approval

    request_config: dict[str, Any] = {
        "sandbox_read_only": sandbox == "read-only",
        "sandbox": sandbox,
        "approval_mode": approval,
        "cwd": str(context.project_dir),
        "summary": "auto",
        "ephemeral": False,
    }
    if base_instructions:
        request_config["base_instructions"] = base_instructions
    collaboration_mode = str(config.get("collaboration_mode") or "").strip().lower()
    if collaboration_mode:
        if collaboration_mode not in {"default", "plan"}:
            raise ValueError("Codex collaboration_mode must be default or plan")
        request_config["collaboration_mode"] = collaboration_mode
    goal_objective = str(config.get("goal_objective") or "").strip()
    if goal_objective:
        request_config["goal_objective"] = goal_objective
    raw_skills = config.get("skills")
    if isinstance(raw_skills, (list, tuple)):
        request_config["skills"] = [
            dict(item) for item in raw_skills if isinstance(item, dict)
        ]
    defaults["config"] = request_config
    return defaults


def apply_runtime_start_request_defaults(
    context: RuntimeLaunchContext,
    request: StartRequest,
) -> StartRequest:
    """Apply deployment-owned launch policy to a direct runtime start.

    AgentKernelWorker already projects these defaults before ``adapter.start``.
    Foreground RunAgent, ``/run_sse`` and OpenAI-compatible routes also create
    ``StartRequest`` objects directly, so they must use the same projection or
    a deployed Codex agent silently falls back to the generic Codex role.

    Request-local config is internal runtime state, not caller-owned policy;
    retain it for local Studio builds while using the manifest model as the
    fallback (and as the fail-closed fallback for an explicit allow-list).
    """

    defaults = kernel_start_request_defaults(context)
    default_model = str(defaults.get("model") or "").strip() or None
    requested_model = str(request.model or "").strip()
    allowed_models = {
        str(item).strip()
        for item in (defaults.get("allowed_models") or [])
        if str(item).strip()
    }
    selected_model = (
        requested_model
        if requested_model and (not allowed_models or requested_model in allowed_models)
        else default_model
    )
    config = {
        **dict(defaults.get("config") or {}),
        **dict(request.config or {}),
    }
    return request.model_copy(
        update={
            "agent_id": request.agent_id or defaults.get("agent_id"),
            "model": selected_model,
            "config": config,
        }
    )


def _manifest_mcp_overrides(config: dict[str, Any]) -> list[str]:
    """Translate declarative MCP bindings into native Codex config keys."""

    overrides: list[str] = []
    servers = config.get("mcp_servers") or []
    if not isinstance(servers, (list, tuple)):
        return overrides
    for server in servers:
        if not isinstance(server, dict):
            continue
        name = str(server.get("name") or "").strip()
        transport = str(
            server.get("transport") or ("http" if server.get("url") else "")
        ).lower()
        url = str(server.get("url") or "").strip()
        if not name:
            continue
        if transport == "stdio":
            command = str(server.get("command") or "").strip()
            if not command:
                continue
            args = [str(argument) for argument in (server.get("args") or [])]
            overrides.append(f"mcp_servers.{name}.command={json.dumps(command)}")
            overrides.append(f"mcp_servers.{name}.args={json.dumps(args)}")
            env_refs = server.get("env_refs") or {}
            if isinstance(env_refs, dict) and env_refs:
                overrides.append(
                    f"mcp_servers.{name}.env_vars="
                    f"{json.dumps(sorted(str(key) for key in env_refs))}"
                )
        elif transport in {"http", "sse"} and url:
            overrides.append(f"mcp_servers.{name}.url={url}")
            env_key = str(server.get("env_key") or "").strip()
            if env_key:
                overrides.append(f"mcp_servers.{name}.bearer_token_env_var={env_key}")
    return overrides


def _manifest_has_network_mcp(config: Mapping[str, Any]) -> bool:
    """Whether a projected Codex MCP binding genuinely needs network access."""

    servers = config.get("mcp_servers") or []
    if not isinstance(servers, (list, tuple)):
        return False
    for server in servers:
        if not isinstance(server, dict):
            continue
        name = str(server.get("name") or "").strip()
        url = str(server.get("url") or "").strip()
        transport = str(server.get("transport") or ("http" if url else "")).lower()
        if name and url and transport in {"http", "sse"}:
            return True
    return False


def _codex_plugin_bootstrap(config: Mapping[str, Any]) -> CodexPluginBootstrap | None:
    """Read the closed, digest-pinned plugin bootstrap launch contract."""

    raw = config.get("codex_plugin_bootstrap")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("codex_plugin_bootstrap must be an object")
    return CodexPluginBootstrap.from_mapping(raw)


_CODEX_HOME_KEY = re.compile(r"^build_[0-9a-f]{8,64}$")


def _codex_home_key(
    config: Mapping[str, Any],
    plugin_bootstrap: CodexPluginBootstrap | None,
) -> str:
    raw = config.get("codex_home_key")
    if raw is None:
        if plugin_bootstrap is not None:
            raise ValueError("plugin-bearing Codex launches require codex_home_key")
        return "unscoped"
    value = str(raw).strip()
    if _CODEX_HOME_KEY.fullmatch(value) is None:
        raise ValueError("codex_home_key must match build_[0-9a-f]{8,64}")
    return value


def _create_codex(context: RuntimeLaunchContext) -> RuntimeAdapter:
    client_factory = context.services.codex_client_factory or AsyncCodexClient
    config = dict(context.config)
    plugin_bootstrap = _codex_plugin_bootstrap(config)
    codex_home_key = _codex_home_key(config, plugin_bootstrap)
    bound_skill_paths: dict[str, str] = {}
    overrides = list(config.get("codex_overrides") or [])
    manifest_mcp_overrides = _manifest_mcp_overrides(config)
    overrides.extend(item for item in manifest_mcp_overrides if item not in overrides)
    projected_config = kernel_start_request_defaults(context).get("config") or {}
    if _manifest_has_network_mcp(config) and projected_config.get("sandbox") == "workspace-write":
        network_override = "sandbox_workspace_write.network_access=true"
        if network_override not in overrides:
            overrides.append(network_override)
    # Studio's default collaboration mode must expose Codex's structured
    # request_user_input tool; the upstream feature is intentionally off by
    # default outside Plan mode.  This stays process-local to the isolated
    # runtime and does not mutate the user's global Codex configuration.
    interaction_override = "features.default_mode_request_user_input=true"
    if interaction_override not in overrides:
        overrides.append(interaction_override)
    try:
        from openai_codex import CodexConfig

        base_cfg = CodexConfig()
        if overrides:
            base_cfg = dataclasses.replace(
                base_cfg,
                config_overrides=tuple(getattr(base_cfg, "config_overrides", ()) or ())
                + tuple(str(o) for o in overrides),
            )
        env = dict(getattr(base_cfg, "env", None) or {})
        isolated_home = _isolated_codex_home(context.project_dir, codex_home_key)
        bound_skill_paths = _materialize_bound_codex_skills(
            isolated_home, config.get("skills")
        )
        env.setdefault("CODEX_HOME", str(isolated_home))
        # HOME 级隔离：codex app-server 还会按约定扫 ~/.agents/skills、
        # ~/.claude/skills 等宿主目录，仅设 CODEX_HOME 挡不住。隔离 HOME 后这些
        # 路径全部落在工作区内，宿主 skills/MCP/凭证不再进入 Agent 上下文。
        # 设 KSADK_CODEX_ISOLATE_HOME=0 可关闭（调试用）。
        if os.environ.get("KSADK_CODEX_ISOLATE_HOME", "1") != "0":
            env.setdefault("HOME", str(isolated_home))
        # 运行期解析出的凭证值（如 MCP bearer_token_env_var 所需）注入子进程环境
        env.update({str(k): str(v) for k, v in (context.config.get("env") or {}).items()})
        base_cfg = dataclasses.replace(base_cfg, env=env)
        try:
            client: Any = client_factory(config=base_cfg)
        except TypeError:
            # 自定义/测试工厂可能不接受 config 参数：退化为无参构造，
            # config_overrides 事后注入（自定义工厂自行承担 env 隔离）。
            client = client_factory()
            if overrides:
                _apply_codex_overrides(client, overrides)
    except ImportError:
        client: Any = client_factory()
        if overrides:
            _apply_codex_overrides(client, overrides)
    timeout = context.config.get("turn_timeout_seconds")
    request_defaults = kernel_start_request_defaults(context)
    request_config = request_defaults.get("config") or {}
    sandbox_read_only = (
        bool(context.config["sandbox_read_only"])
        if "sandbox_read_only" in context.config
        else bool(request_config.get("sandbox_read_only", True))
    )
    return CodexRuntimeAdapter(
        client,
        sandbox_read_only=sandbox_read_only,
        turn_timeout_seconds=float(timeout) if timeout is not None else None,
        bound_skill_paths=bound_skill_paths,
        plugin_bootstrap=plugin_bootstrap,
    )


def _isolated_codex_home(project_dir: Any, codex_home_key: str = "unscoped") -> Path:
    """Use one CODEX_HOME partition per immutable Codex build.

    ``KSADK_CODEX_HOME`` is a base directory, not the final home: every build
    is placed under ``builds/<key>``. Workspace and managed-runtime fallbacks
    use the same separation, so two build digests cannot observe each other's
    App Server plugin cache.
    """

    if codex_home_key != "unscoped" and _CODEX_HOME_KEY.fullmatch(codex_home_key) is None:
        raise ValueError("codex_home_key must match build_[0-9a-f]{8,64}")
    override = os.environ.get("KSADK_CODEX_HOME")
    if override:
        home = Path(override).expanduser() / "builds" / codex_home_key
        home.mkdir(parents=True, exist_ok=True)
        return home

    # Source bundles are deliberately mounted read-only in managed runtimes.
    # Keep the preferred workspace-local isolation for local development, but
    # never make a Codex turn depend on being able to mutate that bundle.
    workspace_home = (
        Path(str(project_dir)) / ".agentkit" / "codex-homes" / codex_home_key
    )
    try:
        workspace_home.mkdir(parents=True, exist_ok=True)
        return workspace_home
    except OSError:
        pass

    # The managed runtime already provides a per-workload writable state
    # volume.  Derive from its explicit directory first, then from the
    # session-store path for backward-compatible images.  /tmp is a final
    # process-local fallback for custom read-only launchers.
    state_dir = os.environ.get("KSADK_RUNTIME_STATE_DIR")
    session_path = os.environ.get("KSADK_SESSION_PATH")
    fallback_root = (
        Path(state_dir)
        if state_dir
        else Path(session_path).expanduser().parent
        if session_path
        else Path(tempfile.gettempdir()) / "ksadk-runtime-state"
    )
    fallback_home = fallback_root / "codex-homes" / codex_home_key
    fallback_home.mkdir(parents=True, exist_ok=True)
    return fallback_home


def _materialize_bound_codex_skills(
    codex_home: Path, value: Any
) -> dict[str, str]:
    """Expose immutable Bundle Skills through Codex's native skill catalog.

    Passing an arbitrary ``SkillInput`` path is accepted by the Python SDK but
    ignored by the real App Server unless the Skill is discoverable by the
    native host.  Copy each content-addressed Bundle Skill into the isolated
    CODEX_HOME so the host owns discovery while the Bundle remains read-only.
    Content-addressed directory names avoid overwriting user-managed Skills
    when an explicit KSADK_CODEX_HOME is used.
    """

    if not isinstance(value, (list, tuple)):
        return {}
    skills_root = codex_home / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    installed: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        raw_path = str(item.get("path") or "").strip()
        if not name or not raw_path:
            continue
        skill_file = Path(raw_path).resolve()
        if skill_file.name != "SKILL.md" or not skill_file.is_file():
            continue
        source_dir = skill_file.parent
        digest = hashlib.sha256()
        for source in sorted(path for path in source_dir.rglob("*") if path.is_file()):
            digest.update(source.relative_to(source_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(source.read_bytes())
            digest.update(b"\0")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "skill"
        target = skills_root / f"ksadk-{digest.hexdigest()[:16]}-{safe_name}"
        if target.is_dir():
            installed[name] = str(target / "SKILL.md")
            continue
        staging = Path(tempfile.mkdtemp(prefix=".ksadk-skill-", dir=skills_root))
        try:
            shutil.copytree(source_dir, staging, dirs_exist_ok=True)
            try:
                staging.replace(target)
            except FileExistsError:
                pass
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        installed[name] = str(target / "SKILL.md")
    return installed


def _apply_codex_overrides(client: Any, overrides: Any) -> None:
    """Append MCP server --config overrides to the client's CodexConfig."""
    try:
        from openai_codex import CodexConfig
    except ImportError:
        return
    # ksadk AsyncCodexClient -> AsyncCodex -> inner AsyncCodexClient.config
    inner_codex = getattr(client, "_codex", None)
    inner_client = getattr(inner_codex, "_client", None) or inner_codex
    cfg = getattr(inner_client, "config", None)
    if cfg is None or not isinstance(cfg, CodexConfig):
        return
    base_overrides = tuple(getattr(cfg, "config_overrides", ()) or ())
    new_overrides = tuple(str(o) for o in overrides if o not in base_overrides)
    if not new_overrides:
        return
    new_cfg = dataclasses.replace(cfg, config_overrides=base_overrides + new_overrides)
    setattr(inner_client, "config", new_cfg)


def _create_framework_runner(context: RuntimeLaunchContext, runtime_type: str) -> BaseRunner:
    try:
        from ksadk.runners.patch_langchain import apply_patch

        apply_patch()
    except ImportError:
        pass
    detection = context.detection
    if detection is None:
        raise ValueError(f"{runtime_type} runtime requires framework detection")
    if context.services.runner_factory is not None:
        runner = context.services.runner_factory(detection, str(context.project_dir))
    else:
        # 复用 runners.factory.create_runner:它优先读 detection.runner_class
        # (agentengine.yaml 的 runner_class 字段),否则按 detection.type 分发到
        # ADKRunner/LangGraphRunner 等。直接硬编码 LangGraphRunner 会绕过自定义
        # runner,导致 root_agent=None 的项目在 preflight 阶段误报"不是有效 CompiledGraph"。
        from ksadk.runners.factory import create_runner

        runner = create_runner(detection, str(context.project_dir))
    if not isinstance(runner, BaseRunner):
        raise TypeError(
            f"{runtime_type} runner factory must return BaseRunner, got {type(runner).__name__}"
        )
    return runner


def _create_adk(context: RuntimeLaunchContext) -> RuntimeAdapter:
    return ADKRuntimeAdapter(_create_framework_runner(context, "adk"))


def _create_langgraph(context: RuntimeLaunchContext) -> RuntimeAdapter:
    return LangGraphRuntimeAdapter(_create_framework_runner(context, "langgraph"))


def build_default_runtime_registry() -> RuntimeRegistry:
    """注册内置 Codex、ADK 和 LangGraph Runtime Factory。"""

    registry = RuntimeRegistry()
    registry.register("codex", _create_codex)
    registry.register("adk", _create_adk)
    registry.register("langgraph", _create_langgraph)
    return registry


def create_runtime_adapter(context: RuntimeLaunchContext) -> RuntimeAdapter:
    """通过唯一默认 Registry 创建 RuntimeAdapter。"""

    return build_default_runtime_registry().create(context)


__all__ = [
    "apply_runtime_start_request_defaults",
    "build_default_runtime_registry",
    "create_runtime_adapter",
    "kernel_start_request_defaults",
]
