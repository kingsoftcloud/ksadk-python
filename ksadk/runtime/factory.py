"""内置 RuntimeAdapter 的唯一默认 Factory Registry。"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

from ksadk.codex.client import AsyncCodexClient
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import RuntimeAdapter, RuntimeRegistry
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter, LangGraphRuntimeAdapter
from ksadk.runtime.launch import RuntimeLaunchContext


def _create_codex(context: RuntimeLaunchContext) -> RuntimeAdapter:
    client_factory = context.services.codex_client_factory or AsyncCodexClient
    overrides = list(context.config.get("codex_overrides") or [])
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
        isolated_home = _isolated_codex_home(context.project_dir)
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
    return CodexRuntimeAdapter(
        client,
        sandbox_read_only=bool(context.config.get("sandbox_read_only", True)),
        turn_timeout_seconds=float(timeout) if timeout is not None else None,
    )


def _isolated_codex_home(project_dir: Any) -> Path:
    """Studio 运行 Agent 使用工作区级隔离 CODEX_HOME。

    避免 codex app-server 继承宿主环境（~/.codex / ~/.wework/codex）里的
    skills、MCP 配置、插件与凭证，污染 Agent 上下文甚至泄漏宿主能力。
    可用 KSADK_CODEX_HOME 显式覆盖。
    """
    override = os.environ.get("KSADK_CODEX_HOME")
    if override:
        return Path(override).expanduser()
    home = Path(str(project_dir)) / ".agentkit" / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    return home


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


__all__ = ["build_default_runtime_registry", "create_runtime_adapter"]
