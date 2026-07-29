"""Stable route-group registry for runtime app composition."""

import importlib

from fastapi import APIRouter

health_meta_router = APIRouter()
sessions_router = APIRouter()
sessions_adk_compat_router = APIRouter()
run_router = APIRouter()
openai_compat_router = APIRouter()
workspace_router = APIRouter()
models_router = APIRouter()
feedback_router = APIRouter()
tools_router = APIRouter()
ui_bootstrap_router = APIRouter()
control_router = APIRouter()
builder_router = APIRouter()
debug_router = APIRouter()

GROUP_ROUTERS: dict[str, APIRouter] = {
    "builder": builder_router,
    "control": control_router,
    "debug": debug_router,
    "feedback": feedback_router,
    "models": models_router,
    "openai_compat": openai_compat_router,
    "run": run_router,
    "sessions": sessions_router,
    "sessions_adk_compat": sessions_adk_compat_router,
    "tools": tools_router,
    "ui_bootstrap": ui_bootstrap_router,
    "workspace": workspace_router,
    "health_meta": health_meta_router,
}

_ROUTE_MODULES = (
    "common",
    "sessions",
    "control",
    "workspace",
    "run",
    "misc",
    "openai_compat",
)


def load_route_modules() -> None:
    """Import each domain module once so its decorators populate the routers."""
    package = __package__ or "ksadk.server.routes"
    for module_name in _ROUTE_MODULES:
        importlib.import_module(f"{package}.{module_name}")
