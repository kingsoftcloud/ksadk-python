"""Independent local A2A callee for the selected-storage PostgreSQL gate."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.context_store import PostgresA2AContextStore
from ksadk.a2a.identity import (
    A2AGatewayIdentityMiddleware,
    A2AIngressTargetBinding,
    A2ATrustedIdentityResolver,
)
from ksadk.a2a.platform import JWTGatewayIdentityVerifier
from ksadk.a2a.resume_store import PostgresA2AResumeStateStore
from ksadk.a2a.routes import A2AConfig, add_a2a_protocol_routes
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.a2a.task_store import A2AOwnerContextBuilder, build_a2a_task_store
from ksadk.harness.engine.postgres_checkpointer import create_bounded_postgres_saver
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.sessions import bind_session_service
from ksadk.sessions.postgres_service import PostgresSessionService


async def main():
    settings = json.loads(sys.stdin.readline())
    dsn = settings["dsn"]
    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    saver, pool = await create_bounded_postgres_saver(dsn)
    sessions = PostgresSessionService(
        dsn=dsn,
        namespace=settings["namespace"],
        tenant_id=settings["tenant"],
    )
    contexts, resumes = PostgresA2AContextStore(engine), PostgresA2AResumeStateStore(engine)
    await contexts.initialize()
    await resumes.initialize()
    calls = []
    release = asyncio.Event()

    async def remote(state):
        calls.append(state["messages"][-1].content)
        if settings["slow"]:
            await release.wait()
        return {"messages": [AIMessage(content="remote:" + calls[-1])]}

    graph = StateGraph(MessagesState)
    graph.add_node("remote", remote)
    graph.add_edge(START, "remote")
    graph.add_edge("remote", END)
    runner = LangGraphRunner(
        SimpleNamespace(
            entry_point="agent.py",
            agent_variable="root_agent",
            name="expert",
        ),
        ".",
    )
    runner._agent = graph.compile(checkpointer=saver)
    runner._module = SimpleNamespace(
        ksadk_prepare_state=lambda payload, _context: {
            "messages": [HumanMessage(content=str(payload.get("input", "local proof")))],
        }
    )
    app = FastAPI()
    target = A2AIngressTargetBinding(
        settings["tenant"],
        settings["tenant"],
        "ar-callee-" + settings["identity"],
        "ar-callee-" + settings["identity"],
        "a2a-agent-" + settings["identity"],
    )
    verifier = JWTGatewayIdentityVerifier(settings["gateway_public_key"])
    app.add_middleware(
        A2AGatewayIdentityMiddleware,
        verifier=verifier,
        probe_verifier=verifier,
        target_binding=target,
    )

    @app.middleware("http")
    async def bind_sessions(request, call_next):
        with bind_session_service(sessions):
            return await call_next(request)

    @app.get("/fixture/status")
    async def status():
        return {"calls": calls}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(listener.getsockname()[1])
    add_a2a_protocol_routes(
        app,
        A2AConfig(enabled=True, base_url=url, agent_name="expert"),
        task_adapter=A2ARuntimeTaskAdapter(
            LangGraphRuntimeAdapter(runner),
            runtime_type="langgraph",
            context_store=contexts,
            resume_state_store=resumes,
        ),
        task_store=build_a2a_task_store(engine=engine),
        context_builder=A2AOwnerContextBuilder(
            identity_resolver=A2ATrustedIdentityResolver(target_binding=target),
            allow_unverified_identity=False,
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            lifespan="off",
            timeout_graceful_shutdown=2,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.01)
        print(json.dumps({"url": url}), flush=True)
        await serving
    finally:
        release.set()
        server.should_exit = True
        if not serving.done():
            await serving
        await runner.close()
        await sessions.aclose()
        await pool.close()
        await engine.dispose()
        listener.close()


if __name__ == "__main__":
    asyncio.run(main())
