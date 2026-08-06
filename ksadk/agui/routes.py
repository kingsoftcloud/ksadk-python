"""Factory wiring for the official AG-UI FastAPI endpoint helper."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, cast

from fastapi import FastAPI

from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.agui.config import AGUIConfig, require_agui_dependencies
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter, LangGraphRuntimeAdapter
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


def build_runtime_adapter(runner: BaseRunner, *, runtime_type: str):
    if runtime_type == "langgraph":
        return LangGraphRuntimeAdapter(runner)
    if runtime_type == "adk":
        return ADKRuntimeAdapter(runner)
    return RunnerRuntimeAdapter(runner, runtime_type=runtime_type)


def _fastapi_endpoint_helper() -> Callable[..., Any]:
    """Resolve the optional untyped helper only after the dependency gate."""
    module = import_module("ag_ui_langgraph")
    helper = getattr(module, "add_langgraph_fastapi_endpoint", None)
    if not callable(helper):
        raise RuntimeError("ag-ui-langgraph does not expose add_langgraph_fastapi_endpoint")
    return cast(Callable[..., Any], helper)


def add_ksadk_agui_endpoint(
    app: FastAPI,
    runner: BaseRunner,
    config: AGUIConfig,
    *,
    event_store_factory=None,
    session_service_factory=None,
) -> KsadkAGUIAgent:
    require_agui_dependencies()

    adapter = build_runtime_adapter(runner, runtime_type=config.runtime_type)
    agent = KsadkAGUIAgent(
        name=config.agent_name,
        adapter=adapter,
        event_store_factory=event_store_factory,
        session_service_factory=session_service_factory,
    )
    _fastapi_endpoint_helper()(app, cast(Any, agent), path=config.path)
    return agent


__all__ = ["add_ksadk_agui_endpoint", "build_runtime_adapter"]
