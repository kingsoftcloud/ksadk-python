from __future__ import annotations

import inspect
import json
from typing import Any

from ksadk.tunnel.registry import ServerDescriptor, TunnelRegistry
from ksadk.tunnel.server import ConnectorConnection


class FakeWebSocket:
    def __init__(self, responder=None) -> None:
        self.responder = responder
        self.messages: list[dict[str, Any]] = []

    async def send_text(self, raw: str) -> None:
        message = json.loads(raw)
        self.messages.append(message)
        if message.get("type") == "http_request" and self.responder is not None:
            result = self.responder(message)
            if inspect.isawaitable(result):
                await result


def add_connection(
    registry: TunnelRegistry,
    *,
    agent: str = "agent",
    connector_id: str = "connector",
    descriptors: list[ServerDescriptor] | None = None,
    responder=None,
) -> tuple[ConnectorConnection, FakeWebSocket]:
    websocket = FakeWebSocket(responder)
    connection = ConnectorConnection(
        connector_id=connector_id,
        websocket=websocket,  # type: ignore[arg-type]
        agent_name=agent,
        servers=descriptors or [ServerDescriptor(name="docs")],
    )
    registry.add_connection(connection)
    return connection, websocket


def request_frames(websocket: FakeWebSocket) -> list[dict[str, Any]]:
    return [message for message in websocket.messages if message.get("type") == "http_request"]
