"""Runtime app route composition independent from the default app facade."""

from fastapi import FastAPI

from ksadk.server.factory import RuntimeAppState, get_state
from ksadk.server.routes.common import _register_integrated_routers, _resolve_agent_ui_spec
from ksadk.server.routes.routers import GROUP_ROUTERS, load_route_modules


def configure_runtime_app(app: FastAPI, state: RuntimeAppState, groups: set[str]) -> None:
    """Attach integrated resources and the selected route groups to one app."""
    load_route_modules()
    _configure_route_dependencies()
    _register_integrated_routers(app, state)

    ordered = sorted(group for group in groups if group != "health_meta")
    if "health_meta" in groups:
        ordered.append("health_meta")
    for group in ordered:
        router = GROUP_ROUTERS.get(group)
        if router is not None:
            app.include_router(router)


def _configure_route_dependencies() -> None:
    """Install dynamic providers that always resolve the request-bound app state."""

    import ksadk.conversations as conversation
    from ksadk.server.routes import dependencies
    from ksadk.server.routes.streaming import (
        SSE_HEARTBEAT_INTERVAL_SECONDS,
        _DetachedSSEStream,
        detached_streaming_response,
    )

    dependencies.configure(
        dependencies.ServerRouteDependencies(
            resolve_session_service=lambda: get_state().resolve_session_service(),
            describe_session_backend=lambda: get_state().describe_session_backend(),
            resolve_agent_ui_spec=_resolve_agent_ui_spec,
            conversation=lambda: conversation,
            detached_streaming_response=detached_streaming_response,
            detached_stream_class=lambda: _DetachedSSEStream,
            heartbeat_interval=lambda: SSE_HEARTBEAT_INTERVAL_SECONDS,
            runtime_app=lambda: get_state().app,
        )
    )
