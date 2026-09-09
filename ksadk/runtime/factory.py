"""内置 RuntimeAdapter 的唯一默认 Factory Registry。"""

from __future__ import annotations

from typing import Any

from ksadk.plugins.providers.codex_native import (
    _apply_codex_overrides as _apply_codex_overrides,
)
from ksadk.plugins.providers.codex_native import (
    _codex_home_key as _codex_home_key,
)
from ksadk.plugins.providers.codex_native import (
    _codex_plugin_bootstrap as _codex_plugin_bootstrap,
)
from ksadk.plugins.providers.codex_native import (
    _isolated_codex_home as _isolated_codex_home,
)
from ksadk.plugins.providers.codex_native import (
    _manifest_has_network_mcp as _manifest_has_network_mcp,
)
from ksadk.plugins.providers.codex_native import (
    _manifest_mcp_overrides as _manifest_mcp_overrides,
)
from ksadk.plugins.providers.codex_native import (
    _materialize_bound_codex_skills as _materialize_bound_codex_skills,
)
from ksadk.plugins.providers.codex_native import (
    codex_request_config,
)
from ksadk.plugins.providers.codex_native import (
    create_codex_adapter as _create_codex,
)
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

    defaults["config"] = codex_request_config(context)
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
        str(item).strip() for item in (defaults.get("allowed_models") or []) if str(item).strip()
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
