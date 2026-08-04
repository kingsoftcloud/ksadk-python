"""内置 RuntimeAdapter 的唯一默认 Factory Registry。"""

from __future__ import annotations

from ksadk.codex.client import AsyncCodexClient
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import RuntimeAdapter, RuntimeRegistry
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter, LangGraphRuntimeAdapter
from ksadk.runtime.launch import RuntimeLaunchContext


def _create_codex(context: RuntimeLaunchContext) -> RuntimeAdapter:
    client_factory = context.services.codex_client_factory or AsyncCodexClient
    client = client_factory()
    timeout = context.config.get("turn_timeout_seconds")
    return CodexRuntimeAdapter(
        client,
        sandbox_read_only=bool(context.config.get("sandbox_read_only", True)),
        turn_timeout_seconds=float(timeout) if timeout is not None else None,
    )


def _create_framework_runner(context: RuntimeLaunchContext, runtime_type: str) -> BaseRunner:
    detection = context.detection
    if detection is None:
        raise ValueError(f"{runtime_type} runtime requires framework detection")
    if context.services.runner_factory is not None:
        runner = context.services.runner_factory(detection, str(context.project_dir))
    elif runtime_type == "adk":
        from ksadk.runners.adk_runner import ADKRunner

        runner = ADKRunner(detection, str(context.project_dir))
    else:
        from ksadk.runners.langgraph_runner import LangGraphRunner

        runner = LangGraphRunner(detection, str(context.project_dir))
    if not isinstance(runner, BaseRunner):
        raise TypeError(
            f"{runtime_type} runner factory must return BaseRunner, "
            f"got {type(runner).__name__}"
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
