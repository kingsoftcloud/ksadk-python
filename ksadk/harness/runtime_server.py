"""Harness Runtime 独立进程入口。

Local Deployment 以 uvicorn 启动本服务。控制面通过 ``/health``、
``/control/activate`` 管理实例；数据面通过 ``/runs`` 驱动同一套
``ManagedLangGraphEngine``，支持 JSON 聚合响应与 RuntimeEvent v2 SSE。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
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


@dataclass
class CheckpointStack:
    """一次装配产出的 Checkpointer 与配套本地状态。"""

    checkpointer: Any
    durable: bool
    run_store: DeploymentRunStore | None
    receipt_store: Any | None
    _checkpointer_context: Any | None = None

    async def aclose(self) -> None:
        if self._checkpointer_context is not None:
            await self._checkpointer_context.__aexit__(None, None, None)
            self._checkpointer_context = None
        if self.receipt_store is not None:
            self.receipt_store.close()
            self.receipt_store = None


async def assemble_checkpoint_stack(
    *,
    state_dir: str | Path | None,
    dsn: str | None = None,
) -> CheckpointStack:
    """按部署形态装配 Checkpointer 与配套状态（runtime_server 与 Provider 共用）。

    分档：显式/环境 DSN → PostgreSQL；有状态目录 → 每 Workspace SQLite
    （checkpoint + RunHandle 索引 + ToolReceipt）；皆无 → 内存回退（非 durable）。
    """

    resolved_dsn = (dsn or os.getenv("KSADK_CHECKPOINT_DSN", "")).strip()
    from ksadk.harness.tool_receipts import ToolReceiptStore

    run_store = DeploymentRunStore(state_dir) if state_dir else None
    receipt_store = (
        ToolReceiptStore(str(Path(state_dir) / "tool_receipts.sqlite")) if state_dir else None
    )
    if resolved_dsn:
        from ksadk.harness.engine.postgres_checkpointer import postgres_checkpointer

        context = postgres_checkpointer(resolved_dsn)
        checkpointer = await context.__aenter__()
        return CheckpointStack(
            checkpointer=checkpointer,
            durable=True,
            run_store=run_store,
            receipt_store=receipt_store,
            _checkpointer_context=context,
        )
    if state_dir is None:
        from ksadk.harness.engine.langgraph import memory_checkpointer

        return CheckpointStack(
            checkpointer=memory_checkpointer(),
            durable=False,
            run_store=None,
            receipt_store=None,
        )
    from ksadk.harness.engine.langgraph import sqlite_checkpointer

    context = sqlite_checkpointer(str(Path(state_dir) / "checkpoints.sqlite"))
    checkpointer = await context.__aenter__()
    return CheckpointStack(
        checkpointer=checkpointer,
        durable=True,
        run_store=run_store,
        receipt_store=receipt_store,
        _checkpointer_context=context,
    )


class DeploymentRunStore:
    """Local durable index for RunHandle and RuntimeEvent v2 projections.

    LangGraph owns checkpoint state. This index only preserves the opaque handle,
    externally queryable status and emitted event projection needed to attach to
    that checkpoint after a process restart.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / "runs.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def load(self) -> tuple[dict[str, dict[str, Any]], dict[str, RunHandle]]:
        if not self.path.is_file():
            return {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw_runs = payload.get("runs") or {}
            raw_handles = payload.get("handles") or {}
            if not isinstance(raw_runs, dict) or not isinstance(raw_handles, dict):
                raise TypeError("runs and handles must be objects")
            runs = {str(key): dict(value) for key, value in raw_runs.items()}
            handles = {
                str(key): RunHandle.model_validate(value) for key, value in raw_handles.items()
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid deployed run index: {self.path}") from exc
        return runs, handles

    def save(
        self,
        runs: dict[str, dict[str, Any]],
        handles: dict[str, RunHandle],
    ) -> None:
        payload = {
            "schemaVersion": 1,
            "runs": runs,
            "handles": {
                run_id: handle.model_dump(mode="json") for run_id, handle in handles.items()
            },
        }
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)


class DeploymentRuntime:
    """One deployed HarnessSpec and its live Agent Loop handles."""

    def __init__(
        self,
        *,
        deployment_id: str,
        spec_payload: dict[str, Any],
        reasoner: Any | None = None,
        engine: Any | None = None,
        engine_kwargs: dict[str, Any] | None = None,
        activated: bool = False,
        state_dir: str | Path | None = None,
    ) -> None:
        self.deployment_id = deployment_id
        self.spec = HarnessSpec.model_validate(spec_payload)
        self._reasoner = reasoner
        self.engine = engine
        self._engine_kwargs = dict(engine_kwargs or {})
        self.activated = activated
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._run_store = DeploymentRunStore(self._state_dir) if self._state_dir else None
        self._checkpointer_context: Any | None = None
        self._owned_receipt_store: Any | None = None
        self._compiled: CompiledHarness | None = None
        self._compile_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()
        if self._run_store is None:
            self._runs, self._handles = {}, {}
        else:
            self._runs, self._handles = self._run_store.load()

    async def initialize(self) -> None:
        if self.engine is not None:
            return
        stack = await assemble_checkpoint_stack(state_dir=self._state_dir)
        self._checkpointer_context = stack._checkpointer_context
        self._owned_receipt_store = stack.receipt_store
        engine_kwargs = dict(self._engine_kwargs)
        if stack.receipt_store is not None and "capability_runtime" not in engine_kwargs:
            from ksadk.harness.capability_runtime import CapabilityRuntime

            engine_kwargs["capability_runtime"] = CapabilityRuntime(
                receipts=stack.receipt_store
            )
        self.engine = compose_engine(
            self.spec,
            reasoner=self._reasoner,
            checkpointer=stack.checkpointer,
            **engine_kwargs,
        )

    async def shutdown(self) -> None:
        if self._checkpointer_context is not None:
            await self._checkpointer_context.__aexit__(None, None, None)
            self._checkpointer_context = None
        if self._owned_receipt_store is not None:
            self._owned_receipt_store.close()
            self._owned_receipt_store = None

    async def ensure_compiled(self) -> CompiledHarness:
        await self.initialize()
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
        await self._persist()
        return handle

    async def resume(self, run_id: str, payload: ResumeRequest) -> RunHandle:
        handle = await self._require_handle(run_id)
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
        await self._persist()
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
        handle = await self._require_handle(run_id)
        result = await self.engine.cancel(handle)
        if result == CancelResult.INTERRUPTED_ACTIVE_TURN:
            self._runs[run_id]["status"] = "canceled"
        elif result == CancelResult.PENDING_CANCEL_RECORDED:
            self._runs[run_id]["status"] = "cancel_requested"
        await self._persist()
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

    async def _require_handle(self, run_id: str) -> RunHandle:
        try:
            handle = self._handles[run_id]
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"unknown or terminal run: {run_id}",
            ) from None
        await self.ensure_compiled()
        is_attached = getattr(self.engine, "is_handle_attached", None)
        if callable(is_attached) and not is_attached(handle):
            try:
                await self.engine.attach(handle, self._compiled)
            except Exception as exc:  # noqa: BLE001 - map engine recovery to API contract
                raise HTTPException(
                    status_code=409,
                    detail=f"run cannot be recovered from checkpoint: {run_id}",
                ) from exc
        return handle

    async def _record(self, handle: RunHandle, events: list[RuntimeEvent]) -> dict[str, Any]:
        record = self._runs[handle.run_id]
        record["events"].extend(_event_payload(event) for event in events)
        status = _status_for(events)
        if status != "running":
            record["status"] = status
        if status in {"completed", "failed", "canceled"}:
            self._handles.pop(handle.run_id, None)
            await self.engine.close(handle)
        await self._persist()
        return record

    async def _persist(self) -> None:
        if self._run_store is None:
            return
        async with self._store_lock:
            self._run_store.save(self._runs, self._handles)


def build_deployment_app(
    *,
    deployment_id: str,
    route: str,
    spec_payload: dict[str, Any],
    build_id: str = "",
    content_hash: str = "",
    reasoner: Any | None = None,
    engine: Any | None = None,
    engine_kwargs: dict[str, Any] | None = None,
    activated: bool = False,
    state_dir: str | Path | None = None,
) -> FastAPI:
    """Build one deployable Runtime service."""

    runtime = DeploymentRuntime(
        deployment_id=deployment_id,
        spec_payload=spec_payload,
        reasoner=reasoner,
        engine=engine,
        engine_kwargs=engine_kwargs,
        activated=activated,
        state_dir=state_dir,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.initialize()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(title="KsADK Harness Runtime", version="1.0.0", lifespan=lifespan)
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
    parser.add_argument("--state-dir", default=os.getenv("KSADK_HARNESS_STATE_DIR", ""))
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
        state_dir=args.state_dir or None,
    )
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
