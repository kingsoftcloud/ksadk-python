"""Loopback-only official TCK business fixture using production protocol components.

Implements the messageId scenarios in a2aproject/a2a-tck 263b9cfa, rather than
asking a model to guess test responses. This is a protocol gate, not managed
identity or native-runtime evidence; those have separate real-network tests.
"""

import asyncio
import os
import sys
from pathlib import Path

import uvicorn
from a2a.server.agent_execution import AgentExecutor
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes, create_rest_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from fastapi import FastAPI
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.routing import Mount

from ksadk.a2a.card import build_agent_card
from ksadk.a2a.executor import _enqueue_initial_task
from ksadk.a2a.protocol_errors import ProtocolRequestHandler, correct_cancel_rest_route
from ksadk.a2a.server import _DurableRequestContextBuilder
from ksadk.a2a.task_store import build_a2a_task_store


class TckBusinessExecutor(AgentExecutor):
    async def execute(self, context, event_queue):
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        prefix = context.message.message_id
        if prefix.startswith("tck-message-response"):
            await event_queue.enqueue_event(
                updater.new_agent_message([Part(text="Direct message response")])
            )
            return
        if context.current_task is None:
            await _enqueue_initial_task(context, event_queue)
        if prefix.startswith("tck-input-required"):
            await updater.requires_input()
            return
        await updater.start_work()
        if prefix.startswith("test-resubscribe-message-id"):
            await asyncio.sleep(4)
        if prefix.startswith("tck-stream-artifact-chunked"):
            await updater.add_artifact(parts=[Part(text="chunk-1 ")], artifact_id="chunks")
            await updater.add_artifact(
                parts=[Part(text="chunk-2")], artifact_id="chunks", append=True, last_chunk=True
            )
        elif prefix.startswith("tck-artifact-file-url"):
            await updater.add_artifact(
                parts=[
                    Part(
                        url="https://example.com/output.txt",
                        media_type="text/plain",
                        filename="output.txt",
                    )
                ]
            )
        elif prefix.startswith(("tck-artifact-file", "tck-stream-artifact-file")):
            await updater.add_artifact(
                parts=[Part(raw=b"tck", media_type="text/plain", filename="output.txt")]
            )
        elif prefix.startswith("tck-artifact-data"):
            data = ParseDict({"key": "value", "count": 42}, Value())
            await updater.add_artifact(parts=[Part(data=data)])
        else:
            texts = {
                "tck-artifact-text": "Generated text content",
                "tck-stream-artifact-text": "Streamed text content",
                "tck-stream-ordering-001": "Ordered output",
                "tck-stream-001": "Stream hello from TCK",
                "tck-stream-003": "Stream task lifecycle",
            }
            for key, value in texts.items():
                if prefix.startswith(key):
                    await updater.add_artifact(parts=[Part(text=value)])
                    break
        await updater.complete(updater.new_agent_message([Part(text="Hello from TCK")]))

    async def cancel(self, context, event_queue):
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


async def main():
    root, port = Path(sys.argv[1]), int(sys.argv[2])
    root.mkdir(parents=True, exist_ok=True)
    store = build_a2a_task_store(
        dsn=os.environ.get("KSADK_A2A_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{root}/tasks.db"),
        create_table=True,
    )
    card = build_agent_card(name="r85_tck", base_url=f"http://127.0.0.1:{port}", skills=["tck"])
    handler = ProtocolRequestHandler(
        agent_executor=TckBusinessExecutor(),
        task_store=store,
        agent_card=card,
        request_context_builder=_DurableRequestContextBuilder(store),
    )
    app = FastAPI()
    rest = [
        correct_cancel_rest_route(route)
        for route in create_rest_routes(handler, path_prefix="/a2a/v1")
    ]
    rest = [route for route in rest if not (isinstance(route, Mount) and route.path == "/{tenant}")]
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a/jsonrpc"),
        rest_routes=rest,
    )
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False)).serve()


if __name__ == "__main__":
    asyncio.run(main())
