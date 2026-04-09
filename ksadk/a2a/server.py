"""High-level server helpers for exposing a KsADK runner over A2A."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette

from ksadk.a2a.card_builder import AgentCardBuilder
from ksadk.a2a.executor import KsAgentExecutor


class KsA2AServer:
    """Expose a KsADK runner as an A2A-compatible ASGI app."""

    def __init__(
        self,
        runner: Any,
        app_name: str,
        url: str,
        description: str = "",
        skills: Sequence[str] | None = None,
        version: str = "1.0.0",
    ) -> None:
        self.runner = runner
        self.app_name = app_name
        self.url = url
        self.description = description
        self.skills = list(skills or [])
        self.version = version

        self.agent_card = AgentCardBuilder(
            name=app_name,
            url=url,
            description=description,
            skills=self.skills,
            version=version,
        ).build()
        self.executor = KsAgentExecutor(runner=runner)
        self.task_store = InMemoryTaskStore()
        self.request_handler = DefaultRequestHandler(
            agent_executor=self.executor,
            task_store=self.task_store,
        )
        self.application = A2AStarletteApplication(
            agent_card=self.agent_card,
            http_handler=self.request_handler,
        )

    def build(self, **kwargs: Any) -> Starlette:
        """Build the Starlette application with the standard A2A routes."""
        return self.application.build(**kwargs)

    def add_routes_to_app(self, app: Starlette) -> None:
        """Attach the A2A routes to an existing Starlette application."""
        self.application.add_routes_to_app(app)


def to_a2a(
    runner: Any,
    app_name: str,
    url: str,
    description: str = "",
    skills: Sequence[str] | None = None,
    version: str = "1.0.0",
) -> KsA2AServer:
    """Convenience helper for building a KsA2AServer."""
    return KsA2AServer(
        runner=runner,
        app_name=app_name,
        url=url,
        description=description,
        skills=skills,
        version=version,
    )
