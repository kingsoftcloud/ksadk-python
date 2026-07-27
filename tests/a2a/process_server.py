"""Standalone A2A process used by process-level interoperability tests."""

from __future__ import annotations

import argparse
from typing import Any

import httpx
import uvicorn
from a2a.client import A2ACardResolver
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict

from ksadk.a2a import (
    A2AConfig,
    A2AControlPlane,
    A2AExternalTransport,
    A2ARoute,
    A2ARouteInterface,
    A2ARuntimeTaskAdapter,
    A2ASpaceClient,
    A2ATarget,
    CredentialInjection,
    DiscoveredAgent,
    PreparedA2AOperation,
    SpaceAgentPage,
    add_a2a_protocol_routes,
)
from ksadk.events.store import RuntimeEventStore
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.sessions.in_memory import InMemorySessionService

SPACE_ID = "a2a-space-00000000000040008000000000000031"
HOSTED_AGENT_ID = "a2a-agent-00000000000040008000000000000032"
EXTERNAL_AGENT_ID = "a2a-agent-00000000000040008000000000000033"
HOSTED_VERSION_ID = "a2a-version-00000000000040008000000000000034"
EXTERNAL_VERSION_ID = "a2a-version-00000000000040008000000000000035"
TASK_ID = "a2a-task-00000000000040008000000000000036"


class EchoRunner:
    async def invoke(self, input_data: dict[str, Any]) -> dict[str, str]:
        return {"output": f"echo:{input_data['input']}"}

    async def stream(self, input_data: dict[str, Any]):
        yield {"delta": "echo:", "type": "text"}
        yield {"delta": str(input_data["input"]), "type": "text"}
        yield {"output": f"echo:{input_data['input']}", "type": "final"}


class StaticBackend(A2AControlPlane):
    def __init__(self, agent: DiscoveredAgent, credential: str) -> None:
        self.agent = agent
        self.credential = credential

    async def list_space_agents(self, space_id: str, **_: Any) -> SpaceAgentPage:
        assert space_id == SPACE_ID
        return SpaceAgentPage(agents=[self.agent])

    async def prepare_call(self, **_: Any) -> PreparedA2AOperation:
        card = MessageToDict(self.agent.agent_card)
        interface = card["supportedInterfaces"][0]
        return PreparedA2AOperation(
            platform_task_id=TASK_ID,
            target=A2ATarget(self.agent.agent_id, self.agent.version_id, ""),
            route=A2ARoute(
                kind="hosted_gateway" if self.agent.source == "hosted" else "external_public",
                interface=A2ARouteInterface(
                    url=interface["url"],
                    protocol_binding=interface["protocolBinding"],
                    protocol_version=interface["protocolVersion"],
                ),
            ),
            call_permit="local-permit",
            call_permit_expires_at="2099-01-01T00:00:00Z",
            credential_handle="target-token" if self.agent.source == "external" else None,
        )

    async def prepare_task_operation(self, **_: Any) -> PreparedA2AOperation:
        raise NotImplementedError

    async def bind_remote_task(self, **_: Any) -> dict[str, Any]:
        return {"A2ATaskId": TASK_ID, "AlreadyBound": False}

    async def append_task_events(self, **kwargs: Any) -> dict[str, Any]:
        return {"AcceptedCount": len(kwargs["events"]), "DuplicateCount": 0}

    async def resolve_credential(self, **_: Any) -> CredentialInjection:
        if self.agent.source == "external" and self.credential:
            return CredentialInjection(headers={"Authorization": f"Bearer {self.credential}"})
        return CredentialInjection()

    def gateway_token(self) -> str:
        return "local-gateway-token"


class StaticExternalTransport(A2AExternalTransport):
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    def client_for_route(
        self,
        route: A2ARouteInterface,
        *,
        route_kind: str,
    ) -> httpx.AsyncClient:
        return self.client


def build_app(*, port: int, name: str, database_path: str, required_token: str) -> FastAPI:
    app = FastAPI()
    auth_headers: list[str] = []
    sessions = InMemorySessionService()
    event_store = RuntimeEventStore(sessions)

    @app.on_event("startup")
    async def initialize_event_session() -> None:
        await sessions.create_session(name, "a2a_space", SPACE_ID)

    @app.middleware("http")
    async def require_a2a_credential(request: Request, call_next):
        if required_token and request.url.path.startswith("/a2a/"):
            authorization = request.headers.get("authorization", "")
            auth_headers.append(authorization)
            if authorization != f"Bearer {required_token}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.post("/test/invoke")
    async def invoke_target(payload: dict[str, Any]) -> dict[str, Any]:
        target_url = str(payload["target_url"])
        async with httpx.AsyncClient() as discovery_http:
            card = await A2ACardResolver(discovery_http, target_url).get_agent_card()
        target_id = str(payload["target_id"])
        target_source = str(payload["target_source"])
        agent = DiscoveredAgent(
            agent_id=EXTERNAL_AGENT_ID if target_source == "external" else HOSTED_AGENT_ID,
            version_id=(EXTERNAL_VERSION_ID if target_source == "external" else HOSTED_VERSION_ID),
            source=target_source,
            agent_card=card,
            route_kind="external_public" if target_source == "external" else "hosted_gateway",
        )
        async with httpx.AsyncClient() as outbound_http:
            client = A2ASpaceClient(
                SPACE_ID,
                StaticBackend(agent, str(payload.get("credential") or "")),
                egress_enabled=True,
                httpx_client=outbound_http,
                external_transport=StaticExternalTransport(outbound_http),
                event_sink=event_store,
            )
            await client.discover()
            task = await client.send_message(agent.agent_id, str(payload["message"]))
        events = await event_store.list(SPACE_ID, invocation_id=task.id)
        return {
            "source": name,
            "target": target_id,
            "task_id": task.id,
            "event_types": [event.event_type for event in events],
            "texts": [event.payload.get("text", "") for event in events],
        }

    @app.get("/test/events")
    async def list_events(
        after_seq_id: int = Query(default=0),
        limit: int | None = Query(default=None),
    ) -> dict[str, Any]:
        events = await event_store.list(SPACE_ID, after_seq_id=after_seq_id)
        if limit is not None:
            events = events[:limit]
        return {"events": [event.to_dict() for event in events]}

    @app.get("/test/auth")
    async def auth_evidence() -> dict[str, Any]:
        return {"authorization": auth_headers}

    runner = EchoRunner()
    add_a2a_protocol_routes(
        app,
        runner,
        A2AConfig(
            enabled=True,
            base_url=f"http://127.0.0.1:{port}",
            agent_name=name,
            skills=["echo"],
            task_store_dsn=f"sqlite+aiosqlite:///{database_path}",
            create_table=True,
        ),
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner, runtime_type="test"), runtime_type="test"
        ),
    )

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--require-token", default="")
    args = parser.parse_args()
    uvicorn.run(
        build_app(
            port=args.port,
            name=args.name,
            database_path=args.database,
            required_token=args.require_token,
        ),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
