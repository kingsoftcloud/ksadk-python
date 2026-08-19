# -*- coding: utf-8 -*-
"""Phase 1 Task 8 hosted-authority canary runtime app.

与旧 canary_app.py 的区别：

- 通过 ``build_agent_kernel_runtime`` 组合根（经
  ``bootstrap_agent_kernel_runtime_from_env``）装配 worker / lease heartbeat /
  RecoveryCoordinator / readiness，而不是手工拼 AgentKernel。
- ``AGENT_KERNEL_AUTHORITY_MODE=hosted``：permit 只信 Server JWKS
  （``AGENT_CONTROL_JWKS_URL``），本地不自签、不合并本地公钥，fail closed。
- RuntimeAdapter 是 ``FakeCodexRuntimeAdapter``：以 codex 语义（live
  JSON-RPC approval 通道）演示 Interaction 全链路——**不是真实 Codex 后端**；
  回包经 ``CodexInteractionProvider`` 用原 ``call_id`` 打回同一 adapter 实例。
  另支持 langgraph 语义（checkpoint resume）演示 durable_resume。evidence
  如实记录 provider 为 fake/mock。

Env（Deployment 注入）：
- AGENT_KERNEL_ENABLED=1 / AGENT_KERNEL_STORE_DRIVER=postgres
- AGENT_KERNEL_STORE_DSN（Secret agent-kernel-postgres）
- AGENT_CONTROL_JWKS_URL（Server 只读 JWKS）
- AGENT_KERNEL_AUTHORITY_MODE=hosted / AGENT_CONTROL_PERMIT_ISSUER
- AGENT_KERNEL_CONTRACT_DIGEST / AGENT_BUNDLE_DIGEST
- POD_UID（downward API）→ activation owner
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ksadk.events.canonical import (
    ApprovalRequest,
    RunProgress,
    RunStarted,
    SourceRef,
    StructuredInputRequest,
    InteractionRequested,
)
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix
from ksadk.kernel.control import default_capability_matrix
from ksadk.runtime.adapter import (
    CancelResult,
    PauseResult,
    ResumePayload,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)

CONTRACT_DIGEST = os.environ.get("AGENT_KERNEL_CONTRACT_DIGEST", "")
BUNDLE_DIGEST = os.environ.get("AGENT_BUNDLE_DIGEST", "phase1-canary-v4")

app = FastAPI(title="agent-kernel-phase1-canary-hosted")

_state: dict[str, object] = {}


# ---------------------------------------------------------------------------
# Fake runtime adapter
# ---------------------------------------------------------------------------


def _input_text(request: StartRequest) -> str:
    """从 RunAgent 的 Messages/ResponsesInput 提取纯文本用于场景选择。"""

    data = request.input
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        parts: list[str] = []
        for item in data:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and isinstance(
                            block.get("text"), str
                        ):
                            parts.append(block["text"])
        return " ".join(parts)
    if isinstance(data, dict):
        return str(data.get("input") or data.get("text") or "")
    return ""


class _FakeFrameworkRuntime:
    """最小 RuntimeDescriptor：native_capabilities 留空（诚实默认）。"""

    runtime_type = "codex"

    def native_capabilities(self) -> dict:
        return {}


def _native(reason: str = "") -> RuntimeCapability:
    return RuntimeCapability(supported=True, mode="native")


def _unavailable(reason: str) -> RuntimeCapability:
    return RuntimeCapability(supported=False, mode="unavailable", reason=reason)


class FakeFrameworkRuntimeAdapter(RuntimeAdapter):
    """Codex / LangGraph 语义的 fake adapter（JSON-RPC mock）。

    ``start`` 按 input 文本选择演示框架：
    - 含 "langgraph"：durable_resume 演示。stream 先发 StructuredInputRequest
      （interrupt + checkpoint），回包经 ``LangGraphInteractionProvider`` 走
      ``adapter.resume(checkpoint)`` 恢复同 thread。
    - 其余（默认）：live_submit 演示。stream 发 ApprovalRequest，回包经
      ``CodexInteractionProvider`` 以原 call_id 经 ``adapter.submit`` 送达。

    回包到达（submit/resume）后 stream 续跑至完成。全部状态在进程内
    （thread 表语义），Pod 重启后无法 attach —— RecoveryCoordinator 会得到
    确定性 interrupted，这正是 12 场景之一期望的行为。
    """

    def __init__(self) -> None:
        super().__init__(_FakeFrameworkRuntime())
        self.submits: list[dict] = []
        self.resumes: list[dict] = []
        self._gates: dict[str, asyncio.Event] = {}

    def capabilities(self) -> RuntimeCapabilityMatrix:
        return RuntimeCapabilityMatrix(
            cancel=_native(),
            pause=_native(),
            resume=_native(),
            submit_interaction=_native(),
            attach=_unavailable("fake_adapter_process_local"),
            steer=_unavailable("runtime_no_native_steer"),
            inject=_unavailable("runtime_no_native_inject"),
            checkpoint=_unavailable("fake_adapter_no_checkpoint_store"),
            durable_restore=_unavailable("fake_adapter_process_local"),
        )

    async def start(self, request: StartRequest) -> RunHandle:
        text = _input_text(request).lower()
        framework = "langgraph" if "langgraph" in text else "codex"
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        self._gates[run_id] = asyncio.Event()
        native_ref = {
            "framework": framework,
            "thread_id": f"thread-{uuid.uuid4().hex[:12]}",
        }
        if framework == "langgraph":
            native_ref["checkpoint_id"] = f"ckpt-{uuid.uuid4().hex[:12]}"
        return RunHandle(
            run_id=run_id,
            session_id=request.session_id,
            runtime_type=framework,
            native_ref=native_ref,
        )

    def stream(self, handle: RunHandle) -> object:
        run_id = handle.run_id
        framework = str(handle.native_ref.get("framework") or "codex")
        source = SourceRef(framework=framework)
        gate = self._gates.setdefault(run_id, asyncio.Event())

        async def _events():
            yield RunStarted(
                schema_version=2,
                event_id=f"{run_id}-started",
                seq=0,
                timestamp=time.time(),
                run_id=run_id,
                scope_id=f"run:{run_id}",
                status="running",
                source=source,
            )
            if framework == "langgraph":
                yield InteractionRequested(
                    schema_version=2,
                    event_id=f"{run_id}-interrupt",
                    seq=0,
                    timestamp=time.time(),
                    run_id=run_id,
                    scope_id=f"run:{run_id}",
                    interaction_id=f"int-{uuid.uuid4().hex[:12]}",
                    interaction_kind="structured_input",
                    request=StructuredInputRequest.model_validate(
                        {
                            "prompt": "请填写表单字段",
                            "schema": {
                                "type": "object",
                                "properties": {"answer": {"type": "string"}},
                                "required": ["answer"],
                            },
                        }
                    ),
                    source=source,
                )
            else:
                yield InteractionRequested(
                    schema_version=2,
                    event_id=f"{run_id}-approval",
                    seq=0,
                    timestamp=time.time(),
                    run_id=run_id,
                    scope_id=f"run:{run_id}",
                    interaction_id=f"int-{uuid.uuid4().hex[:12]}",
                    interaction_kind="approval",
                    request=ApprovalRequest(
                        call_id=f"call-{uuid.uuid4().hex[:12]}",
                        kind="command_execution",
                        detail={"command": "echo phase1-canary"},
                    ),
                    source=source,
                )
            # 等待回包（submit/resume）送达，模拟阻塞在 approval/interrupt。
            await gate.wait()
            yield RunProgress(
                schema_version=2,
                event_id=f"{run_id}-progress",
                seq=0,
                timestamp=time.time(),
                run_id=run_id,
                scope_id=f"run:{run_id}",
                status="running",
                progress=1.0,
                message=f"{framework} fake turn resumed after interaction",
                source=source,
            )

        return _events()

    async def submit(self, handle: RunHandle, payload: object) -> None:
        if isinstance(payload, ResumePayload):
            data = {
                "kind": payload.kind,
                "call_id": payload.call_id,
                "data": payload.data,
            }
        else:
            data = {"kind": "raw", "data": payload}
        self.submits.append({"handle": handle.run_id, **data})
        self._gates.setdefault(handle.run_id, asyncio.Event()).set()

    async def resume(
        self, handle: RunHandle, target: object, payload: object
    ) -> RunHandle:
        self.resumes.append(
            {
                "handle": handle.run_id,
                "target_kind": getattr(target, "kind", None),
                "target_id": getattr(target, "id", None),
                "payload_kind": getattr(payload, "kind", None),
                "payload_data": getattr(payload, "data", None),
            }
        )
        self._gates.setdefault(handle.run_id, asyncio.Event()).set()
        return handle

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self._gates.setdefault(handle.run_id, asyncio.Event()).set()
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def pause(self, handle: RunHandle) -> PauseResult:
        return PauseResult.PAUSED_ACTIVE_TURN

    async def checkpoint(self, handle: RunHandle) -> None:
        raise NotImplementedError

    async def close(self, handle: RunHandle) -> None:
        return None


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


from ksadk.kernel.ingress import (
    KERNEL_INGRESS_SUBMIT_PATH,
    agent_kernel_router,
    kernel_ingress_enabled,
)

if kernel_ingress_enabled():
    app.include_router(agent_kernel_router())

    @app.middleware("http")
    async def _ensure_kernel_session(request, call_next):
        """canonical submit 前确保 session 存在（共享 event log 前置条件）。

        生产 hosted runtime 由 runtime service 负责会话目录；canary 没有该
        层，这里用同一 DSN 的 SessionService 补齐。
        """
        if request.method == "POST" and request.url.path == KERNEL_INGRESS_SUBMIT_PATH:
            body = await request.body()
            try:
                session_id = str(
                    (json.loads(body) or {}).get("command", {}).get("session_id") or ""
                )
            except Exception:
                session_id = ""
            service = _state.get("session_service")
            if session_id and service is not None:
                try:
                    if await service.get_session(session_id) is None:
                        await service.create_session(
                            agent_id=os.environ.get("AGENT_INSTANCE_ID", "phase1-canary-1"),
                            user_id="phase1-canary",
                            session_id=session_id,
                        )
                except Exception:
                    pass

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive
        return await call_next(request)


@app.on_event("startup")
async def startup() -> None:
    from ksadk.kernel.bootstrap import (
        bootstrap_agent_kernel_runtime_from_env,
        get_agent_kernel_runtime,
    )

    runtime = await bootstrap_agent_kernel_runtime_from_env(
        adapter_provider=FakeFrameworkRuntimeAdapter,
    )
    if runtime is None:
        raise RuntimeError("hosted canary requires AGENT_KERNEL_ENABLED=1")
    _state["runtime"] = runtime
    # canary 会话目录：postgres 用同一 DSN 的 SessionService；memory 复用
    # runtime 内部的 InMemorySessionService（同一实例才能被 event store 看到）。
    dsn = os.environ.get("AGENT_KERNEL_STORE_DSN", "").strip()
    if dsn:
        from ksadk.sessions.postgres_service import PostgresSessionService

        service = PostgresSessionService(dsn=dsn)
        await service._ensure_pool()
        _state["session_service"] = service
    else:
        memory_service = getattr(runtime.session_events, "_service", None)
        if memory_service is not None:
            _state["session_service"] = memory_service


@app.on_event("shutdown")
async def shutdown() -> None:
    from ksadk.kernel.bootstrap import get_agent_kernel_runtime

    runtime = get_agent_kernel_runtime()
    if runtime is not None:
        await runtime.close()


@app.get("/healthz")
async def healthz() -> JSONResponse:
    from ksadk.kernel.bootstrap import get_agent_kernel_runtime

    runtime = get_agent_kernel_runtime()
    if runtime is None:
        return JSONResponse(status_code=503, content={"ok": False, "reason": "not_booted"})
    health = await runtime.readiness.check()
    ok = bool(health.get("ready"))
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "instance": os.environ.get("AGENT_INSTANCE_ID", ""), **health},
    )


@app.post("/test/expire-interaction")
async def expire_interaction(body: dict) -> dict:
    """测试钩子：确定性演练 ledger expiry（生产 expiry sweep 不在 Phase 1 范围）。

    直接在当前 activation guard 下调用 store.expire；仍走同一事务性
    ledger transition + terminal SessionEvent 路径。
    """
    from ksadk.kernel.bootstrap import get_agent_kernel_runtime
    from ksadk.kernel.store import ActivationLeaseRequest, ActivationWriteGuard

    runtime = get_agent_kernel_runtime()
    if runtime is None:
        return JSONResponse(status_code=503, content={"error": "not_booted"})
    store = runtime.kernel_store
    session_id = str(body["session_id"])
    lease = await store.current_lease(runtime.config.agent_instance_id, session_id)
    if lease is None:
        lease = await store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id=runtime.config.agent_instance_id,
                session_id=session_id,
                activation_id=f"expire-hook-{uuid.uuid4().hex[:8]}",
                runtime_type="canary-echo",
                bundle_digest=BUNDLE_DIGEST,
                capability_digest="phase1-canary",
                lease_ttl_seconds=30.0,
            )
        )
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )
    receipt = await store.expire(
        str(body["interaction_id"]), int(body["expected_revision"]), guard=guard
    )
    return receipt.model_dump(mode="json")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
