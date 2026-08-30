"""Factory wiring for the official AG-UI FastAPI endpoint helper."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, cast

from fastapi import FastAPI

from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.agui.config import AGUIConfig, require_agui_dependencies
from ksadk.runtime.adapter import RuntimeLaunchContext
from ksadk.runtime.executor import RuntimeExecutor


def _fastapi_endpoint_helper() -> Callable[..., Any]:
    """Resolve the optional untyped helper only after the dependency gate."""
    module = import_module("ag_ui_langgraph")
    helper = getattr(module, "add_langgraph_fastapi_endpoint", None)
    if not callable(helper):
        raise RuntimeError("ag-ui-langgraph does not expose add_langgraph_fastapi_endpoint")
    return cast(Callable[..., Any], helper)


def add_ksadk_agui_endpoint(
    app: FastAPI,
    executor: RuntimeExecutor,
    launch_context: RuntimeLaunchContext,
    config: AGUIConfig,
    *,
    event_store_factory=None,
    session_service_factory=None,
) -> KsadkAGUIAgent:
    require_agui_dependencies()

    agent = KsadkAGUIAgent(
        name=config.agent_name,
        executor=executor,
        launch_context=launch_context,
        event_store_factory=event_store_factory,
        session_service_factory=session_service_factory,
    )
    _fastapi_endpoint_helper()(app, cast(Any, agent), path=config.path)
    return agent


__all__ = ["add_ksadk_agui_endpoint"]
