# -*- coding: utf-8 -*-
"""Task 8 Step 1: 入口收敛失败测试。

五个 surface（run / responses / agui / a2a / studio）在 kernel 路径开启时：
- mutation 只产生恰好一次 ``kernel.submit``；
- 零直接 executor 调用；
- SSE/reconnect cursor 源自同一 Session seq（``SessionEventSubscription.after_seq``）；
- receipt -> HTTP 状态走 ``RECEIPT_HTTP_STATUS`` 唯一映射表。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.kernel import ingress
from ksadk.kernel.contracts import (
    AgentControlReceipt,
    SessionEventEnvelope,
)
from ksadk.runtime.launch import RuntimeLaunchContext
from ksadk.sessions.in_memory import InMemorySessionService

TENANT = "tenant-1"
AGENT_INSTANCE = "agent-1"


class CountingExecutor:
    """任何对旧 executor facade 的调用都计数并视为违规。"""

    def __init__(self) -> None:
        self.direct_calls = 0

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _forbidden(*args: Any, **kwargs: Any) -> Any:
            self.direct_calls += 1
            raise AssertionError(f"direct executor call in kernel route: {name}")

        return _forbidden


def _envelope(seq: int, event_type: str, payload: dict[str, Any]) -> SessionEventEnvelope:
    return SessionEventEnvelope(
        event_id=uuid.uuid4(),
        session_id="s1",
        seq=seq,
        timestamp="2026-08-18T00:00:00Z",
        family="runtime",
        family_version=2,
        event_type=event_type,
        payload=payload,
    )


class RecordingKernel:
    """记录 submit / subscribe 的最小 kernel 桩。"""

    def __init__(self) -> None:
        self.submits: list[Any] = []
        self.subscriptions: list[Any] = []

    async def submit(self, command: Any, *, permit: Any) -> AgentControlReceipt:
        self.submits.append(command)
        return AgentControlReceipt(
            command_id=command.command_id,
            status="accepted",
            message_id=uuid.uuid4(),
            accepted_seq=1,
        )

    async def subscribe(self, subscription: Any, *, permit: Any) -> Any:
        self.subscriptions.append(subscription)
        yield _envelope(
            subscription.after_seq + 1,
            "item.updated",
            {"item_kind": "message", "delta": "hello"},
        )
        yield _envelope(
            subscription.after_seq + 2,
            "run.completed",
            {"output_text": "hello"},
        )


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        self.tmp_path = tmp_path
        self.kernel = RecordingKernel()
        self.executor = CountingExecutor()
        self.launch_context = RuntimeLaunchContext(
            runtime_type="langgraph", project_dir=tmp_path
        )
        self.session_service = InMemorySessionService()
        monkeypatch.setenv("KSADK_AGENT_KERNEL", "1")
        # 注册进程级 kernel（monkeypatch 会在 teardown 还原为 None）。
        monkeypatch.setattr(ingress, "_kernel", self.kernel, raising=True)
        # Studio additionally requires an exact Build/runtime binding before
        # it is allowed to enter Kernel ingress.  Register that trusted
        # binding instead of relying on the obsolete "any active kernel"
        # behaviour this convergence test predates.
        from ksadk.kernel import bootstrap

        monkeypatch.setattr(
            bootstrap,
            "_runtime",
            SimpleNamespace(
                config=SimpleNamespace(
                    tenant_id=TENANT,
                    agent_instance_id=AGENT_INSTANCE,
                    launch_context=self.launch_context,
                    start_request_defaults={"agent_id": AGENT_INSTANCE},
                )
            ),
            raising=True,
        )

    @property
    def submit_count(self) -> int:
        return len(self.kernel.submits)

    @property
    def direct_executor_calls(self) -> int:
        return self.executor.direct_calls

    async def invoke(self, surface: str, *, session_id: str, idempotency_key: str) -> Any:
        if surface == "run":
            return await self._invoke_run(session_id, idempotency_key)
        if surface == "responses":
            return await self._invoke_responses(session_id, idempotency_key)
        if surface == "agui":
            return await self._invoke_agui(session_id, idempotency_key)
        if surface == "a2a":
            return await self._invoke_a2a(session_id, idempotency_key)
        if surface == "studio":
            return await self._invoke_studio(session_id, idempotency_key)
        raise ValueError(surface)

    # ------------------------------------------------------------- surfaces

    async def _invoke_run(self, session_id: str, idempotency_key: str) -> Any:
        from ksadk.server.routes import dependencies as deps
        from ksadk.server.routes import run as run_routes
        from ksadk.server.routes.models import RunAgentActionRequest

        self._orig_get_runtime_execution = run_routes.get_runtime_execution
        self._orig_resolve_session_service = deps.resolve_session_service
        run_routes.get_runtime_execution = lambda: (self.executor, self.launch_context)
        deps.resolve_session_service = lambda: self.session_service
        try:
            request = RunAgentActionRequest(
                AgentId="agent-1",
                Messages=[{"role": "user", "content": "hi"}],
                SessionId=session_id,
                Metadata={"IdempotencyKey": idempotency_key},
            )
            return await run_routes.run_agent_action(request)
        finally:
            run_routes.get_runtime_execution = self._orig_get_runtime_execution
            deps.resolve_session_service = self._orig_resolve_session_service

    async def _invoke_responses(self, session_id: str, idempotency_key: str) -> Any:
        from ksadk.server.routes import dependencies as deps
        from ksadk.server.routes import openai_compat as responses_routes

        self._orig_get_runtime_execution = responses_routes.get_runtime_execution
        self._orig_resolve_session_service = deps.resolve_session_service
        responses_routes.get_runtime_execution = lambda: (
            self.executor,
            self.launch_context,
        )
        deps.resolve_session_service = lambda: self.session_service
        try:
            request = responses_routes.ResponsesRequest(
                input=[{"role": "user", "content": "hi"}],
                session_id=session_id,
                metadata={"idempotency_key": idempotency_key},
            )
            return await responses_routes.responses(request)
        finally:
            responses_routes.get_runtime_execution = self._orig_get_runtime_execution
            deps.resolve_session_service = self._orig_resolve_session_service

    async def _invoke_agui(self, session_id: str, idempotency_key: str) -> Any:
        agui_core = pytest.importorskip(
            "ag_ui.core", reason="AG-UI optional dependency is not installed"
        )
        RunAgentInput = agui_core.RunAgentInput

        from ksadk.agui.agent import KsadkAGUIAgent

        agent = KsadkAGUIAgent(
            name="kernel-agent",
            executor=self.executor,
            launch_context=self.launch_context,
            event_store_factory=None,
            session_service_factory=None,
        )
        events = []
        async for event in agent.run(
            RunAgentInput(
                threadId=session_id,
                runId=idempotency_key,
                state={},
                messages=[],
                tools=[],
                context=[],
                forwardedProps={},
            )
        ):
            events.append(event)
        return events

    async def _invoke_a2a(self, session_id: str, idempotency_key: str) -> Any:
        from ksadk.a2a.executor import A2ARuntimeExecutor

        executor = A2ARuntimeExecutor(task_adapter=_StubTaskAdapter())
        return await executor.execute(
            _StubRequestContext(session_id, idempotency_key),
            _StubEventQueue(),
        )

    async def _invoke_studio(self, session_id: str, idempotency_key: str) -> Any:
        from ksadk.studio.contracts import RunStatus
        from ksadk.studio.run_service import StudioRunService, StudioRunSpec
        from ksadk.studio.workspace import Workspace

        service = StudioRunService(Workspace(self.tmp_path / "studio-ws"), self.executor)
        spec = StudioRunSpec(
            launch_context=self.launch_context,
            build_id="build-1",
            agent_id="agent-1",
            request_config={"idempotency_key": idempotency_key},
        )
        record = await service.run(spec, "hi", session_id=session_id)
        assert record.status == RunStatus.COMPLETED
        return record


class _StubTaskAdapter:
    """kernel 路径不应触碰 task_adapter 的 runtime 方法。"""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"direct task_adapter call in kernel route: {name}")

        return _forbidden


class _StubRequestContext:
    def __init__(self, session_id: str, task_id: str) -> None:
        self.task_id = task_id
        self.context_id = session_id
        self.current_task = None
        self.message = None

    def get_user_input(self) -> str:
        return "hi"


class _StubEventQueue:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.parametrize("surface", ["run", "responses", "agui", "a2a", "studio"])
async def test_every_surface_submits_one_agent_control_command(
    surface, monkeypatch, tmp_path
):
    harness = Harness(monkeypatch, tmp_path)
    await harness.invoke(surface, session_id="s1", idempotency_key="same")
    assert harness.submit_count == 1
    assert harness.direct_executor_calls == 0
    command = harness.kernel.submits[0]
    assert command.idempotency_key == "same"
    assert command.session_id == "s1"
    assert command.tenant_id
    assert command.agent_instance_id
    assert command.authorization_ref
    assert command.source.kind in {
        "studio", "responses", "agui", "a2a", "run", "system",
    }


async def test_run_surface_cursor_comes_from_session_seq(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    response = await harness.invoke("run", session_id="s1", idempotency_key="k1")
    subscription = harness.kernel.subscriptions[0]
    assert subscription.after_seq >= 1  # receipt.accepted_seq
    assert subscription.session_id == "s1"
    body = response.body if hasattr(response, "body") else response
    assert b"hello" in body or "hello" in str(body)


async def test_receipt_http_status_mapping():
    from ksadk.kernel.contracts import ControlError

    def receipt(status: str) -> AgentControlReceipt:
        kwargs: dict[str, Any] = {}
        if status in ("accepted", "duplicate"):
            kwargs["message_id"] = uuid.uuid4()
        else:
            kwargs["error"] = ControlError(code=status, message=status, retryable=False)
        return AgentControlReceipt(command_id=uuid.uuid4(), status=status, **kwargs)

    for status, expected in ingress.RECEIPT_HTTP_STATUS.items():
        assert ingress.receipt_http_status(receipt(status)) == expected


async def test_kernel_disabled_by_default(monkeypatch):
    monkeypatch.delenv("KSADK_AGENT_KERNEL", raising=False)
    assert not ingress.kernel_ingress_enabled()
    assert not ingress.kernel_route_active()


def test_map_run_request_carries_runtime_options_model():
    """RunAgent 的 Model 覆盖必须进入 command payload(透传链第一跳)。"""
    from ksadk.kernel import ingress

    trusted = ingress.trusted_context(
        source_kind="system",
        source_ref="idem-model",
        session_id="s-model",
        operations=("enqueue",),
    )
    command = ingress.map_run_request(
        trusted=trusted,
        session_id="s-model",
        idempotency_key="idem-model",
        content=[{"type": "input_text", "text": "hi"}],
        runtime_options={"model": "glm-5.3"},
    )
    assert command.payload["runtime_options"] == {"model": "glm-5.3"}

    plain = ingress.map_run_request(
        trusted=trusted,
        session_id="s-model",
        idempotency_key="idem-model-2",
        content=[{"type": "input_text", "text": "hi"}],
    )
    assert "runtime_options" not in plain.payload
