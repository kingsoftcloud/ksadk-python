"""OS-process worker for real PostgreSQL A2A recovery tests.

Every action goes through an actual loopback HTTP server and the official A2A
client.  In particular, ``recover`` reaches ``A2ARuntimeExecutor``, attaches the
persisted ``RunHandle``, and calls ``RuntimeAdapter.resume``; it never edits a
Task directly.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import socket
from typing import Any, AsyncIterator

import httpx
import uvicorn
from a2a.client import ClientCallContext, ClientConfig, create_client
from a2a.types import (
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
)
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict

from ksadk.a2a import A2AConfig, A2ARuntimeTaskAdapter, add_a2a_protocol_routes
from ksadk.a2a.card import build_agent_card
from ksadk.events import EventPhase, EventType, RuntimeEvent
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)


class _ProcessRuntime(BaseRuntime):
    runtime_type = "process-test"

    def native_capabilities(self) -> dict[str, Any]:
        return {"Durable": True, "SharedAcrossProcesses": True}


class _DurableProcessAdapter(RuntimeAdapter):
    """Minimal durable adapter used to prove the A2A recovery orchestration."""

    def __init__(self) -> None:
        super().__init__(_ProcessRuntime())
        self._attached: set[str] = set()
        self._resumed: dict[str, Any] = {}
        self.attach_calls: list[str] = []
        self.resume_calls: list[str] = []

    @staticmethod
    def _token(run_id: str, session_id: str) -> str:
        return hashlib.sha256(f"{run_id}:{session_id}:ksadk-pg-recovery".encode()).hexdigest()

    async def start(self, request: StartRequest) -> RunHandle:
        run_id = str(request.metadata.get("invocation_id") or "")
        checkpoint_id = f"checkpoint-{run_id}"
        handle = RunHandle(
            run_id=run_id,
            session_id=request.session_id,
            runtime_type=self.runtime.runtime_type,
            native_ref={
                "checkpoint_id": checkpoint_id,
                "known_checkpoint_ids": [checkpoint_id],
                "pending_approval_ids": ["approval-1"],
                "durable_token": self._token(run_id, request.session_id),
            },
        )
        self._attached.add(run_id)
        return handle

    def is_handle_attached(self, handle: RunHandle) -> bool:
        return handle.run_id in self._attached

    async def attach(self, handle: RunHandle) -> RunHandle:
        expected = self._token(handle.run_id, handle.session_id)
        if handle.runtime_type != self.runtime.runtime_type:
            raise ValueError("persisted runtime type does not match process adapter")
        if handle.native_ref.get("durable_token") != expected:
            raise ValueError("persisted run handle has an invalid durable token")
        self._attached.add(handle.run_id)
        self.attach_calls.append(handle.run_id)
        return handle

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        if not self.is_handle_attached(handle):
            raise RuntimeError("resume called before durable attach")
        if target.id not in {
            handle.run_id,
            str(handle.native_ref.get("checkpoint_id") or ""),
        }:
            raise ValueError("resume target is not owned by this run")
        self._resumed[handle.run_id] = payload.data if payload else None
        self.resume_calls.append(handle.run_id)
        return handle

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        async def events() -> AsyncIterator[RuntimeEvent]:
            common = {
                "agent_id": "pg-recovery-agent",
                "user_id": "a2a",
                "session_id": handle.session_id,
                "invocation_id": handle.run_id,
            }
            if handle.run_id not in self._resumed:
                yield RuntimeEvent.create(
                    EventType.TEXT_COMPLETED,
                    seq_id=1,
                    phase=EventPhase.COMMENTARY.value,
                    payload={"text": "durable draft"},
                    **common,
                )
                yield RuntimeEvent.create(
                    EventType.APPROVAL_REQUESTED,
                    seq_id=2,
                    payload={
                        "approval_id": "approval-1",
                        "call_id": "approval-1",
                        "kind": "tool",
                        "detail": {"prompt": "Approve the durable operation?"},
                    },
                    **common,
                )
                yield RuntimeEvent.create(
                    EventType.CHECKPOINT_CREATED,
                    seq_id=3,
                    payload={
                        "checkpoint_id": str(handle.native_ref["checkpoint_id"]),
                        "granularity": "snapshot",
                    },
                    **common,
                )
                yield RuntimeEvent.create(
                    EventType.RUN_INTERRUPTED,
                    seq_id=4,
                    payload={
                        "status": "interrupted",
                        "reason": "approval_required",
                        "prompt": "Approve?",
                    },
                    **common,
                )
                return
            yield RuntimeEvent.create(
                EventType.TEXT_COMPLETED,
                seq_id=5,
                phase=EventPhase.FINAL_ANSWER.value,
                payload={"text": "continued by process B"},
                **common,
            )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                seq_id=6,
                payload={"status": "completed"},
                **common,
            )

        return events()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return (
            CancelResult.PENDING_CANCEL_RECORDED
            if self.is_handle_attached(handle)
            else CancelResult.NOT_RUNNING
        )

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id=str(handle.native_ref.get("checkpoint_id") or handle.run_id),
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=True,
                granularity="snapshot",
                rollback_scope="turn",
                fork_supported=False,
                durable=True,
                shared_across_pods=True,
            ),
            ref=dict(handle.native_ref),
        )

    async def close(self, handle: RunHandle) -> None:
        self._attached.discard(handle.run_id)


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _start_server(app: FastAPI, port: int) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            return server, task
        if task.done():
            await task
        await asyncio.sleep(0.01)
    raise RuntimeError("A2A worker HTTP server did not start")


def _headers(args: argparse.Namespace) -> dict[str, str]:
    headers: dict[str, str] = {}
    if args.account:
        headers["X-Ksc-Account-Id"] = args.account
    if args.runtime:
        headers["X-Auth-Agent-Id"] = args.runtime
    return headers


def _task_dict(task: Task | None) -> dict[str, Any] | None:
    return MessageToDict(task, preserving_proto_field_name=True) if task is not None else None


async def _run(args: argparse.Namespace) -> None:
    port = _unused_port()
    app = FastAPI()
    adapter = _DurableProcessAdapter()
    protocol = add_a2a_protocol_routes(
        app,
        object(),
        A2AConfig(
            enabled=True,
            base_url=f"http://127.0.0.1:{port}",
            agent_name="pg-recovery-agent",
            task_store_dsn=args.dsn,
            create_table=not args.no_create_table,
        ),
        task_adapter=A2ARuntimeTaskAdapter(adapter, runtime_type=adapter.runtime.runtime_type),
    )
    server, server_task = await _start_server(app, port)
    http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    client = await create_client(
        agent=build_agent_card(
            name="pg-recovery-agent",
            base_url=f"http://127.0.0.1:{port}",
        ),
        client_config=ClientConfig(httpx_client=http, streaming=True),
    )
    call_context = ClientCallContext(service_parameters=_headers(args) or None)
    try:
        if args.action == "write":
            request = SendMessageRequest(
                message=Message(
                    role=Role.ROLE_USER,
                    parts=[Part(text="start durable operation")],
                    message_id=f"message-{args.task_id}",
                ),
                configuration=SendMessageConfiguration(return_immediately=False),
            )
            returned_task: Task | None = None
            returned_task_id = ""
            async for item in client.send_message(request, context=call_context):
                task_field = getattr(item, "task", None)
                if str(getattr(task_field, "id", "") or ""):
                    returned_task = task_field
                    returned_task_id = task_field.id
                item_task_id = str(
                    getattr(item, "task_id", "")
                    or getattr(getattr(item, "status_update", None), "task_id", "")
                    or ""
                )
                if item_task_id:
                    returned_task_id = item_task_id
            await asyncio.sleep(0.2)
            task_id = returned_task_id or (returned_task.id if returned_task is not None else "")
            task = None
            for _ in range(50):
                try:
                    task = await client.get_task(GetTaskRequest(id=task_id), context=call_context)
                    if task.status.state != 2:  # submitted/working; keep polling
                        break
                except Exception:
                    await asyncio.sleep(0.05)
            if task is None:
                raise RuntimeError(f"A2A task {task_id!r} was not persisted")
            print(
                json.dumps(
                    {
                        "event": "written",
                        "owner": (
                            f"{args.account}/{args.runtime}"
                            if args.account and args.runtime
                            else "anonymous"
                        ),
                        "port": port,
                        "task": _task_dict(task),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if args.hold:
                await asyncio.Event().wait()
            return

        try:
            before = await client.get_task(GetTaskRequest(id=args.task_id), context=call_context)
        except Exception:
            before = None
        if args.action == "get":
            print(
                json.dumps(
                    {
                        "event": "read",
                        "owner": (
                            f"{args.account}/{args.runtime}"
                            if args.account and args.runtime
                            else "anonymous"
                        ),
                        "port": port,
                        "task": _task_dict(before),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return

        if before is None:
            raise RuntimeError(f"task {args.task_id!r} is not visible to this owner")
        request = SendMessageRequest(
            message=Message(
                role=Role.ROLE_USER,
                parts=[Part(text="approve")],
                message_id=f"resume-{args.task_id}",
                task_id=args.task_id,
            ),
            configuration=SendMessageConfiguration(return_immediately=False),
        )
        async for _ in client.send_message(request, context=call_context):
            pass
        after = await client.get_task(GetTaskRequest(id=args.task_id), context=call_context)
        print(
            json.dumps(
                {
                    "event": "recovered",
                    "owner": (
                        f"{args.account}/{args.runtime}"
                        if args.account and args.runtime
                        else "anonymous"
                    ),
                    "port": port,
                    "before": _task_dict(before),
                    "after": _task_dict(after),
                    "attach_calls": adapter.attach_calls,
                    "resume_calls": adapter.resume_calls,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        await client.close()
        await http.aclose()
        server.should_exit = True
        await server_task
        await protocol.task_store.engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("write", "get", "recover"))
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--account")
    parser.add_argument("--runtime")
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--no-create-table", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_run(_parse_args()))
