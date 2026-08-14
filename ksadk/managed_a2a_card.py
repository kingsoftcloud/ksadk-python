"""Platform-layer discovery-only AgentCard mount for managed A2A Runtime.

v1 scope (this module):
    Mount ``GET /.well-known/agent-card.json`` only. No JSON-RPC, no identity
    middleware, no TaskStore/ContextStore/ResumeStore. The card is available as
    soon as the runtime starts, *before* any A2A Agent is registered, so the
    server-side ``GetAToAAgentCard(hosted)`` probe can fetch it and break the
    registration chicken-and-egg cycle.

    The card mounts whenever ``KSADK_A2A_RUNTIME_ID`` (ar-*, available at deploy
    time) is non-empty. It does **not** depend on ``KSADK_A2A_AGENT_ID``
    (only known after the A2A Agent is registered). skills default to empty;
    :func:`ksadk.a2a_card.build_agent_card` fills a ``general`` skill when none
    are provided.

v2 scope (future, not this module):
    :class:`ksadk.a2a.bootstrap.AgentEngineA2ABootstrap` adds JSON-RPC on top
    of the same card and the same identity env. The bootstrap replaces this
    mount in ``RuntimeAppConfig.a2a`` once shared durable storage and a real
    gateway-forward identity verifier are in place.

Wire 1.0 conformance:
    The card is built with :func:`build_agent_card` (same factory used by the
    full A2A server), so ``supportedInterfaces`` / ``protocolVersion`` /
    ``defaultInputModes`` / ``defaultOutputModes`` match wire 1.0 exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Sequence

from a2a.types import AgentSkill
from fastapi import FastAPI

from ksadk.a2a_card import build_agent_card


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


@dataclass(frozen=True)
class ManagedA2ACardConfig:
    """Database-free configuration for the managed discovery-only route."""

    enabled: bool = True
    base_url: str = "http://127.0.0.1:8000"
    agent_name: str = "agent"
    description: str = ""
    version: str = "0.1.0"
    skills: Sequence[str | AgentSkill] = ()
    streaming: bool = False
    prefer_stream: bool = False


@dataclass(frozen=True)
class ManagedA2ACardMount:
    """Discovery-only card endpoint; mounted by ``RuntimeAppConfig.a2a``.

    Held as a plain dataclass so ``factory._wire_a2a_if_enabled`` can isinstance
    it without importing the (optional) ``AgentEngineA2ABootstrap`` class.
    """

    config: ManagedA2ACardConfig

    def mount(self, app: FastAPI) -> None:
        """Mount ``GET /.well-known/agent-card.json`` into ``app``.

        Directly registers a FastAPI route returning the card JSON, avoiding
        ``a2a.server.routes`` (which pulls in the full A2A server stack and its
        ``culsans`` dependency). The response body is the same AgentCard JSON
        that ``create_agent_card_routes`` would produce.
        """
        card = build_agent_card(
            name=self.config.agent_name,
            base_url=self.config.base_url,
            description=self.config.description,
            version=self.config.version,
            skills=self.config.skills or (),
            streaming=self.config.streaming,
        )

        def _card_to_dict() -> dict:
            # a2a.types.AgentCard is a protobuf message; serialize to the same
            # camelCase JSON wire format the a2a-sdk route produces.
            from google.protobuf.json_format import MessageToDict

            return MessageToDict(
                card,
                preserving_proto_field_name=False,
                use_integers_for_enums=False,
                always_print_fields_with_no_presence=True,
            )


        from fastapi import Response
        from fastapi.responses import JSONResponse

        card_payload = _card_to_dict()

        async def _get_agent_card() -> Response:
            return JSONResponse(content=card_payload)

        app.add_api_route(
            "/.well-known/agent-card.json",
            _get_agent_card,
            methods=["GET"],
            name="a2a_agent_card",
            include_in_schema=False,
        )

    async def start(self) -> None:
        """No stores/middleware to initialize in v1."""

        return None

    async def stop(self) -> None:
        return None


def build_managed_a2a_card_if_configured() -> Optional[ManagedA2ACardMount]:
    """Read the A2A identity env vars and build a discovery-only card mount.

    Returns ``None`` when the runtime is not A2A-capable
    (``KSADK_A2A_RUNTIME_ID`` empty), so ``run_server`` skips mounting entirely.
    Returns a mount even when ``KSADK_A2A_AGENT_ID`` is absent: the discovery
    card is intentionally available *before* the A2A Agent is registered.

    Name/version fallback chain:
        ``KSADK_A2A_AGENT_NAME`` → ``AGENTENGINE_MANAGED_RUNTIME_NAME`` →
        ``KSADK_A2A_RUNTIME_ID``.
    """
    runtime_id = _env("KSADK_A2A_RUNTIME_ID")
    if not runtime_id:
        return None

    base_url = _env("KSADK_A2A_INTERNAL_BASE_URL") or "http://localhost:8080"
    name = _env("KSADK_A2A_AGENT_NAME") or _env("AGENTENGINE_MANAGED_RUNTIME_NAME") or runtime_id
    version = _env("KSADK_A2A_AGENT_VERSION") or "0.1.0"

    config = ManagedA2ACardConfig(
        enabled=True,
        base_url=base_url,
        agent_name=name,
        description="",
        version=version,
        skills=(),
        streaming=False,
        prefer_stream=False,
    )
    return ManagedA2ACardMount(config)


__all__ = [
    "ManagedA2ACardConfig",
    "ManagedA2ACardMount",
    "build_managed_a2a_card_if_configured",
]
