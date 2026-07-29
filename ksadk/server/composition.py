"""Runtime app route composition independent from the default app facade."""

from fastapi import FastAPI

from ksadk.server.factory import RuntimeAppState
from ksadk.server.routes.common import _register_integrated_routers
from ksadk.server.routes.routers import GROUP_ROUTERS, load_route_modules


def configure_runtime_app(app: FastAPI, state: RuntimeAppState, groups: set[str]) -> None:
    """Attach integrated resources and the selected route groups to one app."""
    load_route_modules()
    _register_integrated_routers(app, state)

    ordered = sorted(group for group in groups if group != "health_meta")
    if "health_meta" in groups:
        ordered.append("health_meta")
    for group in ordered:
        router = GROUP_ROUTERS.get(group)
        if router is not None:
            app.include_router(router)
