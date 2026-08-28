"""Harness Runtime 独立进程入口。

Local Deployment 以 uvicorn 启动本服务。控制面通过 ``/health``、
``/control/activate`` 管理实例；数据面通过 ``/runs`` 驱动同一套
``ManagedLangGraphEngine``，支持 JSON 聚合响应与 RuntimeEvent v2 SSE。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict, Field

from ksadk.harness.engine.base import CompiledHarness
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.skill_composition import compose_engine
from ksadk.harness.spec import HarnessSpec
from ksadk.runtime import CancelResult, ResumePayload, ResumeTarget, RunHandle, StartRequest


class RunRequest(BaseModel):
    """Deployed Runtime invocation contract."""

    model_config = ConfigDict(populate_by_name=True)

    input: Any
    user_id: str = Field(alias="userId")
    session_id: str = Field(alias="sessionId")
    agent_id: str | None = Field(default=None, alias="agentId")
    invocation_id: str | None = Field(default=None, alias="invocationId")
    metadata: dict[str, Any] = Field(default_factory=dict)
    stream: bool = False


class ResumeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision: str
    call_id: str | None = Field(default=None, alias="callId")
    stream: bool = False


def _event_payload(event: RuntimeEvent) -> dict[str, Any]:
    return event.to_v2().to_dict()


def _status_for(events: list[RuntimeEvent]) -> str:
    if not events:
        return "running"
    event_type = events[-1].event_type
    if event_type == EventType.RUN_COMPLETED:
        return "completed"
    if event_type == EventType.RUN_FAILED:
        return "failed"
    if event_type == EventType.RUN_CANCELED:
        return "canceled"
    if event_type == EventType.RUN_INTERRUPTED:
        return "awaiting_approval"
    return "running"


class DeploymentRuntime:
    """One deployed HarnessSpec and its live Agent Loop handles."""

    def __init__(
        self,
        *,
        deployment_id: str,
        spec_payload: dict[str, Any],
        reasoner: Any | None = None,
        engine: Any | None = None,
        activated: bool = False,
    ) -> None:
        self.deployment_id = deployment_id
        self.spec = HarnessSpec.model_validate(spec_payload)
        # A deployed process must support approval resume for its lifetime. Durable
        # cross-process recovery remains a platform Store concern; the local runtime
        # uses an in-memory checkpointer instead of silently disabling resume.
        self.engine = engine or compose_engine(
            self.spec,
            reasoner=reasoner,
            checkpointer=InMemorySaver(),
        )
        self.activated = activated
        self._compiled: CompiledHarness | None = None
        self._compile_lock = asyncio.Lock()
        self._handles: dict[str, RunHandle] = {}
        self._runs: dict[str, dict[str, Any]] = {}

    async def ensure_compiled(self) -> CompiledHarness:
        if self._compiled is None:
            async with self._compile_lock:
                if self._compiled is None:
                    self._compiled = await self.engine.compile(self.spec)
        return self._compiled

    async def start(self, payload: RunRequest) -> RunHandle:
        if not self.activated:
            raise HTTPException(status_code=409, detail="deployment is not active")
        if payload.invocation_id and payload.invocation_id in self._runs:
            raise HTTPException(
                status_code=409,
                detail=f"duplicate invocationId: {payload.invocation_id}",
            )
        compiled = await self.ensure_compiled()
        metadata = dict(payload.metadata)
        if payload.invocation_id:
            metadata["invocation_id"] = payload.invocation_id
        handle = await self.engine.start(
            StartRequest(
                input=payload.input,
                user_id=payload.user_id,
                session_id=payload.session_id,
                # Agent Revision refs are control-plane resource identifiers and
                # contain ``/``. Thread IDs require a compact data-plane identity.
                agent_id=payload.agent_id or self.deployment_id,
                metadata=metadata,
            ),
            compiled,
        )
        self._handles[handle.run_id] = handle
        self._runs[handle.run_id] = {"runId": handle.run_id, "status": "running", "events": []}
        return handle

    async def resume(self, run_id: str, payload: ResumeRequest) -> RunHandle:
        handle = self._require_handle(run_id)
        thread_id = str(handle.native_ref.get("thread_id") or "")
        await self.engine.resume(
            handle,
            ResumeTarget(kind="thread_id", id=thread_id),
            ResumePayload(
                kind="approval_decision",
                call_id=payload.call_id,
                data=payload.decision,
            ),
        )
        self._runs[run_id]["status"] = "running"
        return handle

    async def collect(self, handle: RunHandle) -> dict[str, Any]:
        events = [event async for event in self.engine.stream(handle)]
        return await self._record(handle, events)

    async def event_stream(self, handle: RunHandle) -> AsyncIterator[str]:
        events: list[RuntimeEvent] = []
        try:
            async for event in self.engine.stream(handle):
                events.append(event)
                payload = json.dumps(
                    _event_payload(event),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"event: {event.event_type}\ndata: {payload}\n\n"
        finally:
            await self._record(handle, events)

    async def cancel(self, run_id: str) -> dict[str, Any]:
        handle = self._require_handle(run_id)
        result = await self.engine.cancel(handle)
        if result == CancelResult.INTERRUPTED_ACTIVE_TURN:
            self._runs[run_id]["status"] = "canceled"
        elif result == CancelResult.PENDING_CANCEL_RECORDED:
            self._runs[run_id]["status"] = "cancel_requested"
        return {
            "runId": run_id,
            "status": self._runs[run_id]["status"],
            "cancelResult": result.value,
        }

    def get(self, run_id: str) -> dict[str, Any]:
        try:
            return self._runs[run_id]
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}") from None

    def _require_handle(self, run_id: str) -> RunHandle:
        try:
            return self._handles[run_id]
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"unknown or terminal run: {run_id}",
            ) from None

    async def _record(self, handle: RunHandle, events: list[RuntimeEvent]) -> dict[str, Any]:
        record = self._runs[handle.run_id]
        record["events"].extend(_event_payload(event) for event in events)
        status = _status_for(events)
        if status != "running":
            record["status"] = status
        if status in {"completed", "failed", "canceled"}:
            self._handles.pop(handle.run_id, None)
            await self.engine.close(handle)
        return record


def build_deployment_app(
    *,
    deployment_id: str,
    route: str,
    spec_payload: dict[str, Any],
    build_id: str = "",
    content_hash: str = "",
    reasoner: Any | None = None,
    engine: Any | None = None,
    activated: bool = False,
) -> FastAPI:
    """Build one deployable Runtime service."""

    app = FastAPI(title="KsADK Harness Runtime", version="1.0.0")
    runtime = DeploymentRuntime(
        deployment_id=deployment_id,
        spec_payload=spec_payload,
        reasoner=reasoner,
        engine=engine,
        activated=activated,
    )
    app.state.deployment_runtime = runtime

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "deploymentId": deployment_id,
            "route": route,
            "buildId": build_id,
            "activated": runtime.activated,
        }

    @app.get("/manifest")
    async def manifest() -> dict[str, Any]:
        return {
            "deploymentId": deployment_id,
            "route": route,
            "buildId": build_id,
            "contentHash": content_hash,
            "agentRevisionRef": spec_payload.get("agentRevisionRef", ""),
            "modelProfileRef": (spec_payload.get("model") or {}).get("profileRef", ""),
        }

    @app.post("/control/activate")
    async def activate(request: Request) -> dict[str, Any]:
        # The CLI binds this app to loopback. Keep the control endpoint local if an
        # embedding host exposes the ASGI app on another interface.
        if request.client is not None and request.client.host not in {
            "127.0.0.1",
            "::1",
            "testclient",
        }:
            raise HTTPException(status_code=403, detail="activation is local-control only")
        await runtime.ensure_compiled()
        runtime.activated = True
        return {"deploymentId": deployment_id, "status": "active"}

    @app.post("/runs")
    async def runs(payload: RunRequest):
        handle = await runtime.start(payload)
        if payload.stream:
            return StreamingResponse(
                runtime.event_stream(handle),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(await runtime.collect(handle))

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return runtime.get(run_id)

    @app.post("/runs/{run_id}:resume")
    async def resume(run_id: str, payload: ResumeRequest):
        handle = await runtime.resume(run_id, payload)
        if payload.stream:
            return StreamingResponse(runtime.event_stream(handle), media_type="text/event-stream")
        return JSONResponse(await runtime.collect(handle))

    @app.post("/runs/{run_id}:cancel")
    async def cancel(run_id: str) -> dict[str, Any]:
        return await runtime.cancel(run_id)

    return app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ksadk.harness.runtime_server")
    parser.add_argument("--spec-file", required=True)
    parser.add_argument("--route", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--build-id", default="")
    parser.add_argument("--content-hash", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    spec_payload = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
    app = build_deployment_app(
        deployment_id=args.deployment_id,
        route=args.route,
        spec_payload=spec_payload,
        build_id=args.build_id,
        content_hash=args.content_hash,
    )
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
