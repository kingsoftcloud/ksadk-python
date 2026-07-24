"""A2AProtocolRuntime hosted E2E (goal-05)。

覆盖(hosted 单进程,ASGI in-process):
- AgentCard 符合 wire 1.0(supportedInterfaces + A2A-Version)。
- sync(return_immediately=False)roundtrip。
- streaming artifact chunk。
- return_immediately=True 立即返回 working task。
- get_task / subscribe / cancel_task。
- durable TaskStore(sqlite AsyncEngine)重启恢复。

external 端用第二个挂在同一 ASGI app 的 A2A server 充当(hosted→external 由
goal-06 SpaceClient 联调补第四向)。
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    SubscribeToTaskRequest,
    TaskState,
)
from fastapi import FastAPI

from ksadk.a2a import (
    A2AConfig,
    A2ARuntimeTaskAdapter,
    add_a2a_protocol_routes,
    build_agent_card,
)
from ksadk.events import EventPhase, EventType, RuntimeEvent
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


async def _wait_task_in_states(client, states, timeout=6.0) -> str:
    """等 store 里出现一个处于 states 之一的任务,返回其 task_id。

    store 实时反映 execute 的状态(start_work 即持久化 working)。注意:sqlite 在
    并发 ASGI 请求下承载有限,这里**先睡 ~0.5s 让 execute 进入 working 并空闲,
    再低频轮询**,避免与后台 send 流并发打满同一连接(生产用 PG 无此限)。
    """
    await asyncio.sleep(0.5)
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        response = await client.list_tasks(ListTasksRequest())
        for task in response.tasks:
            if task.status.state in states:
                return task.id
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"超时仍未出现状态为 {states} 的任务")
        await asyncio.sleep(0.25)


class _EchoRunner:
    """echo runner:invoke 返回输入;stream 逐块产出。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def invoke(self, input_data):
        self.calls.append(input_data)
        return {"output": f"echo:{input_data['input']}"}

    async def stream(self, input_data):
        self.calls.append(input_data)
        yield {"delta": "echo:", "type": "text"}
        yield {"delta": str(input_data["input"]), "type": "text"}
        yield {"output": f"echo:{input_data['input']}", "type": "final"}


def _build_app(task_dsn: str, runner=None) -> tuple[FastAPI, object]:
    app = FastAPI()
    runner = runner or _EchoRunner()
    config = A2AConfig(
        enabled=True,
        base_url="http://testserver",
        agent_name="echo-agent",
        skills=["echo"],
        task_store_dsn=task_dsn,
        create_table=True,
    )
    server = add_a2a_protocol_routes(
        app,
        runner,
        config,
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner, runtime_type="test"), runtime_type="test"
        ),
    )
    return app, server


async def _client_for(app: FastAPI, card=None, base_url: str = "http://testserver"):
    transport = httpx.ASGITransport(app=app)
    httpx_client = httpx.AsyncClient(transport=transport, base_url=base_url)
    if card is None:
        card = build_agent_card(name="echo-agent", base_url=base_url, skills=["echo"])
    client = await create_client(
        agent=card,
        client_config=ClientConfig(httpx_client=httpx_client, streaming=True),
    )
    return client, httpx_client


def _send(text: str, return_immediately: bool = False) -> SendMessageRequest:
    message = Message(
        role=Role.ROLE_USER,
        parts=[Part(text=text)],
        message_id=f"m-{uuid.uuid4().hex}",
    )
    return SendMessageRequest(
        message=message,
        configuration=SendMessageConfiguration(return_immediately=return_immediately),
    )


async def _close(client, httpx_client) -> None:
    try:
        await client.close()
    finally:
        await httpx_client.aclose()


@pytest.mark.asyncio
async def test_agent_card_wire_1_0(tmp_path):
    app, _ = _build_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        resp = await http.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "echo-agent"
    interfaces = {i["protocolBinding"]: i for i in card["supportedInterfaces"]}
    assert interfaces["JSONRPC"]["protocolVersion"] == "1.0"
    assert interfaces["HTTP+JSON"]["protocolVersion"] == "1.0"
    assert "url" not in card  # 不再有顶层 url(0.3 遗留)


@pytest.mark.asyncio
async def test_sync_send_roundtrip(tmp_path):
    app, _ = _build_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client, httpx_client = await _client_for(app)
    try:
        latest_state = None
        texts: list[str] = []
        async for response in client.send_message(_send("ping", return_immediately=False)):
            if response.task and response.task.id:
                latest_state = response.task.status.state
            if response.status_update:
                latest_state = response.status_update.status.state
            if response.artifact_update and response.artifact_update.artifact.parts:
                texts.extend(p.text for p in response.artifact_update.artifact.parts if p.text)
            if response.message and response.message.parts:
                texts.extend(p.text for p in response.message.parts if p.text)
        assert latest_state == TaskState.TASK_STATE_COMPLETED
        # artifact 是分块(echo: + ping),完成 message 是整段;join 后应含完整 echo。
        assert "echo:ping" in "".join(texts)
    finally:
        await _close(client, httpx_client)


@pytest.mark.asyncio
async def test_streaming_produces_artifact_chunks(tmp_path):
    app, _ = _build_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client, httpx_client = await _client_for(app)
    try:
        artifact_texts: list[str] = []
        async for response in client.send_message(_send("stream", return_immediately=False)):
            if response.artifact_update and response.artifact_update.artifact.parts:
                for part in response.artifact_update.artifact.parts:
                    if part.text:
                        artifact_texts.append(part.text)
        joined = "".join(artifact_texts)
        assert "echo:stream" in joined
    finally:
        await _close(client, httpx_client)


@pytest.mark.asyncio
async def test_return_immediately_returns_working_task(tmp_path):
    app, _ = _build_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client, httpx_client = await _client_for(app)
    try:
        task = None
        async for response in client.send_message(_send("bg", return_immediately=True)):
            if response.task and response.task.id:
                task = response.task
                break
        assert task is not None
        assert task.status.state in (
            TaskState.TASK_STATE_WORKING,
            TaskState.TASK_STATE_SUBMITTED,
        )
    finally:
        await _close(client, httpx_client)


class _BlockingRunner:
    """有界阻塞 runner:stream 阻塞 ~2s,保证 cancel 时 task 仍在 working(已完成不可取消)。"""

    async def invoke(self, input_data):
        await asyncio.sleep(2)
        return {"output": f"echo:{input_data['input']}"}

    async def stream(self, input_data):
        await asyncio.sleep(2)
        yield {"output": f"echo:{input_data['input']}", "type": "final"}


def test_production_routes_require_runtime_adapter(tmp_path):
    """Enabled A2A routes cannot start without a RuntimeAdapter bridge."""
    app = FastAPI()
    config = A2AConfig(
        enabled=True,
        base_url="http://testserver",
        agent_name="invalid-agent",
        task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/t.db",
    )

    with pytest.raises(TypeError, match="task_adapter"):
        add_a2a_protocol_routes(app, _BlockingRunner(), config)


@pytest.mark.asyncio
async def test_subscribe_streams_task_events_to_terminal(tmp_path):
    """client.subscribe(task_id) 持续拿到该 task 的 status/artifact 更新直至 terminal。"""
    runner = _BlockingRunner()
    app, _ = _build_app(f"sqlite+aiosqlite:///{tmp_path}/t.db", runner=runner)
    client, httpx_client = await _client_for(app)

    async def _consume():
        async for _ in client.send_message(_send("sub", return_immediately=False)):
            pass

    send_task = asyncio.create_task(_consume())
    try:
        task_id = await _wait_task_in_states(
            client, {TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_SUBMITTED}
        )
        states: list = []
        artifact_texts: list[str] = []
        async for event in client.subscribe(SubscribeToTaskRequest(id=task_id)):
            if event.status_update:
                states.append(event.status_update.status.state)
            if event.task and event.task.id:
                states.append(event.task.status.state)
            if event.artifact_update and event.artifact_update.artifact.parts:
                artifact_texts.extend(
                    p.text for p in event.artifact_update.artifact.parts if p.text
                )
        assert TaskState.TASK_STATE_COMPLETED in states
    finally:
        send_task.cancel()
        try:
            await send_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await _close(client, httpx_client)


async def _roundtrip_text(client, text: str) -> str:
    """send 一个阻塞 roundtrip,返回 join 后的全部文本。

    文本可能出现在三处:artifact_update.artifact.parts(分块 artifact)、
    status_update.message.parts(完成 message)、message.parts(直接 message 响应)。
    """
    texts: list[str] = []
    async for response in client.send_message(_send(text, return_immediately=False)):
        if response.artifact_update and response.artifact_update.artifact.parts:
            texts.extend(p.text for p in response.artifact_update.artifact.parts if p.text)
        if response.status_update and response.status_update.status.message:
            texts.extend(p.text for p in response.status_update.status.message.parts if p.text)
        if response.message and response.message.parts:
            texts.extend(p.text for p in response.message.parts if p.text)
    return "".join(texts)


class _DelegatingRunner:
    """hosted agent A 的 runner:执行时经 A2A client 调用另一个 hosted agent B。"""

    def __init__(self, target_app: FastAPI, target_card) -> None:
        self._target_app = target_app
        self._target_card = target_card

    async def invoke(self, input_data):
        client, hc = await _client_for(
            self._target_app, card=self._target_card, base_url="http://agent-b"
        )
        try:
            output = await _roundtrip_text(client, input_data["input"])
            return {"output": output}
        finally:
            await _close(client, hc)

    async def stream(self, input_data):
        result = await self.invoke(input_data)
        yield {"output": result["output"], "type": "final"}


@pytest.mark.asyncio
async def test_hosted_to_hosted(tmp_path):
    """hosted→hosted:hosted agent A 的 runner 经 A2A 协议调用 hosted agent B。"""
    # agent B(hosted,echo)
    app_b = FastAPI()
    add_a2a_protocol_routes(
        app_b,
        (runner_b := _EchoRunner()),
        A2AConfig(
            enabled=True,
            base_url="http://agent-b",
            agent_name="agent-b",
            skills=["echo"],
            task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/b.db",
            create_table=True,
        ),
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner_b, runtime_type="test"), runtime_type="test"
        ),
    )
    card_b = build_agent_card(name="agent-b", base_url="http://agent-b", skills=["echo"])
    # agent A(hosted),runner 委托调 B
    app_a = FastAPI()
    add_a2a_protocol_routes(
        app_a,
        (runner_a := _DelegatingRunner(app_b, card_b)),
        A2AConfig(
            enabled=True,
            base_url="http://agent-a",
            agent_name="agent-a",
            task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/a.db",
            create_table=True,
        ),
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner_a, runtime_type="test"), runtime_type="test"
        ),
    )
    client, hc = await _client_for(
        app_a,
        card=build_agent_card(name="agent-a", base_url="http://agent-a"),
        base_url="http://agent-a",
    )
    try:
        output = await _roundtrip_text(client, "hello-b")
        # A 的回复来自 B 的 echo
        assert "echo:hello-b" in output
    finally:
        await _close(client, hc)


@pytest.mark.asyncio
async def test_external_to_hosted(tmp_path):
    """external→hosted:外部 A2A caller 经 CardResolver 发现 + 标准调用触达 hosted agent。"""
    app, _ = _build_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    transport = httpx.ASGITransport(app=app)
    hc = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    try:
        # 外部 caller 不预知 card,走标准 discovery(A2ACardResolver)。
        resolver = A2ACardResolver(httpx_client=hc, base_url="http://testserver")
        card = await resolver.get_agent_card()
        assert card.name == "echo-agent"
        client = await create_client(
            agent=card, client_config=ClientConfig(httpx_client=hc, streaming=True)
        )
        try:
            output = await _roundtrip_text(client, "from-external")
            assert "echo:from-external" in output
        finally:
            await client.close()
    finally:
        await hc.aclose()


class _HitlRuntimeAdapter(RuntimeAdapter):
    """Protocol E2E runtime: first stream interrupts, resume completes same handle."""

    def __init__(self) -> None:
        super().__init__(_NoopRuntime())
        self.handle: RunHandle | None = None
        self.resume_payload: ResumePayload | None = None

    async def start(self, request: StartRequest) -> RunHandle:
        self.handle = RunHandle(
            run_id=str(request.metadata.get("invocation_id") or "run-1"),
            session_id=request.session_id,
            runtime_type="test",
            native_ref={"thread_id": request.session_id},
        )
        return self.handle

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        assert handle == self.handle
        assert target == ResumeTarget(kind="checkpoint_id", id="ck-1")
        self.resume_payload = payload
        return handle

    def stream(self, handle: RunHandle):  # noqa: ANN201
        async def _events():
            if self.resume_payload is None:
                yield RuntimeEvent.create(
                    EventType.RUN_INTERRUPTED,
                    agent_id="hitl-agent",
                    user_id="tenant",
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    seq_id=1,
                    payload={"status": "input_required", "prompt": "需要审批才能继续"},
                )
                yield RuntimeEvent.create(
                    EventType.CHECKPOINT_CREATED,
                    agent_id="hitl-agent",
                    user_id="tenant",
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    seq_id=2,
                    payload={"checkpoint_id": "ck-1", "granularity": "snapshot"},
                )
                return
            yield RuntimeEvent.create(
                EventType.TEXT_COMPLETED,
                agent_id="hitl-agent",
                user_id="tenant",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=3,
                phase=EventPhase.FINAL_ANSWER.value,
                payload={"text": f"approved:{self.resume_payload.data}"},
            )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                agent_id="hitl-agent",
                user_id="tenant",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=4,
                payload={"status": "completed"},
            )

        return _events()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return CancelResult.NOT_RUNNING

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        raise NotImplementedError

    async def close(self, handle: RunHandle) -> None:
        return None


def _send_with_task(text: str, task_id: str) -> SendMessageRequest:
    message = Message(
        role=Role.ROLE_USER,
        parts=[Part(text=text)],
        message_id=f"m-{uuid.uuid4().hex}",
        task_id=task_id,
    )
    return SendMessageRequest(
        message=message,
        configuration=SendMessageConfiguration(return_immediately=False),
    )


@pytest.mark.asyncio
async def test_input_required_then_resume(tmp_path):
    """§7.2:input-required 映射 checkpoint/resume——首发停 input-required(附 resume token),
    带答案 follow-up 续跑至完成。"""
    app = FastAPI()
    runtime_adapter = _HitlRuntimeAdapter()
    add_a2a_protocol_routes(
        app,
        object(),
        A2AConfig(
            enabled=True,
            base_url="http://testserver",
            agent_name="hitl-agent",
            task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/t.db",
            create_table=True,
        ),
        task_adapter=A2ARuntimeTaskAdapter(runtime_adapter, runtime_type="test"),
    )
    client, httpx_client = await _client_for(
        app, card=build_agent_card(name="hitl-agent", base_url="http://testserver")
    )
    try:
        # 1) 首发:任务停 input-required。
        async for _ in client.send_message(_send("开始", return_immediately=False)):
            pass
        task_id = await _wait_task_in_states(client, {TaskState.TASK_STATE_INPUT_REQUIRED})
        task = await client.get_task(GetTaskRequest(id=task_id))
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED

        # 2) follow-up 带答案续跑 → 完成。
        final_state = None
        texts: list[str] = []
        async for response in client.send_message(_send_with_task("批准", task_id)):
            if response.status_update:
                final_state = response.status_update.status.state
                if response.status_update.status.message:
                    texts.extend(
                        p.text for p in response.status_update.status.message.parts if p.text
                    )
            if response.task and response.task.id:
                final_state = response.task.status.state
            if response.artifact_update and response.artifact_update.artifact.parts:
                texts.extend(p.text for p in response.artifact_update.artifact.parts if p.text)
        assert final_state == TaskState.TASK_STATE_COMPLETED
        assert "approved:批准" in "".join(texts)
        assert runtime_adapter.resume_payload == ResumePayload(
            kind="hitl_answer",
            call_id=None,
            data="批准",
        )
    finally:
        await _close(client, httpx_client)


class _RecordingRuntimeAdapter(RuntimeAdapter):
    """记录 cancel 调用的最小 RuntimeAdapter(验证 A2A cancel 走 RuntimeAdapter.cancel)。"""

    def __init__(self) -> None:
        super().__init__(_NoopRuntime())
        self.cancel_handles: list[RunHandle] = []
        self.handle: RunHandle | None = None
        self.cancelled = asyncio.Event()

    async def start(self, request: StartRequest) -> RunHandle:
        self.handle = RunHandle(
            run_id=str(request.metadata.get("invocation_id") or "run-1"),
            session_id=request.session_id,
            runtime_type="test",
            native_ref={"thread_id": request.session_id},
        )
        return self.handle

    def stream(self, handle):  # noqa: ANN201
        async def _events():
            await self.cancelled.wait()
            yield RuntimeEvent.create(
                EventType.RUN_CANCELED,
                agent_id="echo-agent",
                user_id="tenant",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=1,
                payload={"status": "canceled"},
            )

        return _events()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.cancel_handles.append(handle)
        self.cancelled.set()
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(
        self, handle, target: ResumeTarget, payload: ResumePayload | None
    ) -> RunHandle:  # pragma: no cover
        raise NotImplementedError

    async def checkpoint(self, handle) -> CheckpointDescriptor:  # pragma: no cover
        raise NotImplementedError

    async def close(self, handle) -> None:  # pragma: no cover
        return None


class _NoopRuntime(BaseRuntime):
    def native_capabilities(self):  # pragma: no cover
        return {}


@pytest.mark.asyncio
async def test_cancel_routes_through_runtime_adapter(tmp_path):
    """goal-05 硬性要求:A2A cancel 走 RuntimeAdapter.cancel(G0.3),不在 executor 自造。"""
    runner = _BlockingRunner()
    adapter = _RecordingRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(adapter, runtime_type="test")
    app = FastAPI()
    config = A2AConfig(
        enabled=True,
        base_url="http://testserver",
        agent_name="echo-agent",
        task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/t.db",
        create_table=True,
    )
    add_a2a_protocol_routes(app, runner, config, task_adapter=task_adapter)
    client, httpx_client = await _client_for(app)

    async def _consume():
        async for _ in client.send_message(_send("x", return_immediately=False)):
            pass

    send_task = asyncio.create_task(_consume())
    try:
        task_id = await _wait_task_in_states(
            client, {TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_SUBMITTED}
        )
        canceled = await client.cancel_task(CancelTaskRequest(id=task_id))
        assert canceled.status.state == TaskState.TASK_STATE_CANCELED
        # cancel 经 task_adapter 路由到了 RuntimeAdapter.cancel,且带回 RunHandle。
        assert len(adapter.cancel_handles) == 1
        assert adapter.cancel_handles[0].run_id
    finally:
        send_task.cancel()
        try:
            await send_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await _close(client, httpx_client)


@pytest.mark.asyncio
async def test_taskstore_restart_recovery(tmp_path):
    dsn = f"sqlite+aiosqlite:///{tmp_path}/t.db"
    # 第一次:写一个 task。
    app1, _ = _build_app(dsn)
    client1, httpx_client1 = await _client_for(app1)
    task_id = None
    try:
        async for response in client1.send_message(_send("durable", return_immediately=True)):
            if response.task and response.task.id:
                task_id = response.task.id
                break
        assert task_id is not None
    finally:
        await _close(client1, httpx_client1)

    # 模拟重启:新建 app + 新 task store(同一 sqlite 文件),task 仍可恢复。
    app2, _ = _build_app(dsn)
    client2, httpx_client2 = await _client_for(app2)
    try:
        fetched = await client2.get_task(GetTaskRequest(id=task_id))
        assert fetched.id == task_id
    finally:
        await _close(client2, httpx_client2)
