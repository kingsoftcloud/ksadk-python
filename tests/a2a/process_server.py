"""Standalone A2A process used by process-level interoperability tests."""

from __future__ import annotations

import argparse
from typing import Any

import httpx
import uvicorn
from a2a.client import A2ACardResolver
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from ksadk.a2a import (
    A2AConfig,
    A2ARuntimeTaskAdapter,
    A2ASpaceClient,
    DiscoveredAgent,
    SpaceAgentPage,
    SpaceDiscoveryBackend,
    add_a2a_protocol_routes,
)
from ksadk.a2a.credential import StaticCredentialProvider
from ksadk.events.store import RuntimeEventStore
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.sessions.in_memory import InMemorySessionService

SPACE_ID = "process-interop"


class EchoRunner:
    async def invoke(self, input_data: dict[str, Any]) -> dict[str, str]:
        return {"output": f"echo:{input_data['input']}"}

    async def stream(self, input_data: dict[str, Any]):
        yield {"delta": "echo:", "type": "text"}
        yield {"delta": str(input_data["input"]), "type": "text"}
        yield {"output": f"echo:{input_data['input']}", "type": "final"}


class StaticBackend(SpaceDiscoveryBackend):
    def __init__(self, agent: DiscoveredAgent) -> None:
        self.agent = agent

    async def list_space_agents(self, space_id: str, **_: Any) -> SpaceAgentPage:
        assert space_id == SPACE_ID
        return SpaceAgentPage(agents=[self.agent])


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
        handle = "target-token" if payload.get("credential") else None
        agent = DiscoveredAgent(
            agent_id=str(payload["target_id"]),
            version_id="v1",
            source=str(payload["target_source"]),
            agent_card=card,
            credential_handle=handle,
        )
        provider = StaticCredentialProvider(
            {
                "target-token": {
                    "scheme": "bearer",
                    "token": str(payload.get("credential") or ""),
                }
            }
        )
        client = A2ASpaceClient(
            SPACE_ID,
            StaticBackend(agent),
            egress_enabled=True,
            credential_provider=provider,
            event_sink=event_store,
        )
        await client.discover()
        task = await client.send_message(agent.agent_id, str(payload["message"]))
        events = await event_store.list(SPACE_ID, invocation_id=task.id)
        return {
            "source": name,
            "target": agent.agent_id,
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
