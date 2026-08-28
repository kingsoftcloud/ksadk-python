"""MCP 渐进披露接入默认 Managed Agent Loop 的集成测试（P0）。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ksadk.harness.capabilities import CapabilityDescriptor, RiskLevel
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.engine.mcp_disclosure import (
    MCP_CALL_TOOL_TOOL,
    MCP_LIST_TOOLS_TOOL,
    MCP_READ_SCHEMA_TOOL,
)
from ksadk.harness.events import EventType
from ksadk.harness.mcp_runtime import (
    McpCapabilityRuntime,
    McpRuntimeError,
    McpServerBinding,
    McpToolCallContext,
    McpTransport,
)
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)
from ksadk.runtime import StartRequest

_FINANCE = "mcp://finance-tools@1.0.0"
_LOW = "mcp://low-tools@1.0.0"
_HR = "mcp://hr-tools@1.0.0"

_TOOLS = {
    "get_invoice": {
        "name": "get_invoice",
        "description": "按发票号查询发票信息",
        "inputSchema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
        },
    },
    "pay_invoice": {
        "name": "pay_invoice",
        "description": "支付发票（高风险写操作）",
        "inputSchema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}, "amount": {"type": "number"}},
            "required": ["invoice_id", "amount"],
        },
    },
}


class _FakeTransport(McpTransport):
    def __init__(self, tools: dict[str, dict], calls: list[tuple[str, dict]] | None = None):
        self._tools = tools
        self.calls = calls if calls is not None else []

    async def list_tools(self) -> list[dict[str, Any]]:
        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        return {"status": "ok", "tool": name, "echo": arguments}


class _IdempotentTransport(_FakeTransport):
    def __init__(self, tools: dict[str, dict]):
        super().__init__(tools)
        self.contexts: list[McpToolCallContext] = []

    async def call_tool_with_context(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: McpToolCallContext,
    ) -> Any:
        self.contexts.append(context)
        return await self.call_tool(name, arguments)


class _ScriptedReasoner:
    def __init__(self, calls: list[tuple[str, dict]], final_text: str = "完成。"):
        self._calls = calls
        self._final = final_text
        self.step = 0
        self.first_messages: tuple[dict, ...] = ()
        self.tool_names: list[set[str]] = []

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt
        self.tool_names.append({tool.openai_schema["function"]["name"] for tool in tools})
        if self.step == 0:
            self.first_messages = tuple(messages)
        if self.step < len(self._calls):
            name, arguments = self._calls[self.step]
            self.step += 1
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(call_id=f"c{self.step}", name=name, arguments=arguments),
                )
            )
        return HarnessReasoningTurn(final_text=self._final)


def _runtime(*, risk: RiskLevel = RiskLevel.MEDIUM) -> tuple[McpCapabilityRuntime, _FakeTransport]:
    transport = _FakeTransport(_TOOLS)
    runtime = McpCapabilityRuntime()
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=_FINANCE,
                kind="mcp",
                name="财务工具",
                description="发票查询与支付",
                version="1.0.0",
                risk_level=risk,
            ),
            transport=transport,
            required=True,
        )
    )
    return runtime, transport


def _descriptor(*, risk: RiskLevel = RiskLevel.MEDIUM) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=_FINANCE,
        kind="mcp",
        name="财务工具",
        description="发票查询与支付",
        version="1.0.0",
        risk_level=risk,
    )


def _spec(*, risk_binding: bool = True, load_policy: str = "on_demand") -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://finance@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="你是财务助手。"),
        capabilities=CapabilityBindings(
            mcp_bindings=(
                CapabilityBinding(
                    capability_ref=_FINANCE, required=risk_binding, load_policy=load_policy
                ),
            )
        ),
    )


def _drive(reasoner, runtime, spec):
    async def run():
        engine = ManagedLangGraphEngine(reasoner=reasoner, mcp_runtime=runtime)
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                agent_id="a1", user_id="u1", session_id="s1", input="查发票",
                runtime_type="managed-langgraph",
            ),
            compiled,
        )
        events = [e async for e in engine.stream(handle)]
        return engine, events

    return asyncio.run(run())


def test_l0_catalog_in_first_input_and_no_schema_leak():
    reasoner = _ScriptedReasoner([(MCP_LIST_TOOLS_TOOL, {"server_id": _FINANCE})])
    engine, events = _drive(reasoner, _runtime()[0], _spec())
    # L0：首输入只有 Server 名称/描述/风险等级，不含任何 Tool 名。
    catalog_text = str(reasoner.first_messages)
    assert "财务工具" in catalog_text and "发票查询与支付" in catalog_text
    assert "medium" in catalog_text
    assert "get_invoice" not in catalog_text and "pay_invoice" not in catalog_text
    # 三个披露工具可用。
    assert {MCP_LIST_TOOLS_TOOL, MCP_READ_SCHEMA_TOOL, MCP_CALL_TOOL_TOOL}.issubset(
        reasoner.tool_names[0]
    )
    del engine, events


def test_full_chain_l1_l2_l3_emits_events_and_calls_transport():
    transport_holder: dict[str, _FakeTransport] = {}
    runtime, transport = _runtime()
    transport_holder["t"] = transport
    reasoner = _ScriptedReasoner(
        [
            (MCP_LIST_TOOLS_TOOL, {"server_id": _FINANCE}),
            (MCP_READ_SCHEMA_TOOL, {"server_id": _FINANCE, "tool_name": "get_invoice"}),
            (
                MCP_CALL_TOOL_TOOL,
                {"server_id": _FINANCE, "tool_name": "get_invoice",
                 "arguments": {"invoice_id": "INV-1"}},
            ),
        ]
    )
    _engine, events = _drive(reasoner, runtime, _spec())
    disclosed = [e for e in events if e.event_type == EventType.MCP_DISCLOSED]
    assert [(e.payload["level"], e.payload.get("tool_name")) for e in disclosed] == [
        (1, None),
        (2, "get_invoice"),
        (3, "get_invoice"),
    ]
    assert all(str(e.payload["content_hash"]).startswith("sha256:") for e in disclosed)
    # 真实调用到达传输层，参数正确。
    assert transport_holder["t"].calls == [("get_invoice", {"invoice_id": "INV-1"})]
    assert events[-1].event_type == EventType.RUN_COMPLETED


def test_call_without_schema_is_rejected():
    reasoner = _ScriptedReasoner(
        [
            (MCP_LIST_TOOLS_TOOL, {"server_id": _FINANCE}),
            (
                MCP_CALL_TOOL_TOOL,
                {"server_id": _FINANCE, "tool_name": "get_invoice",
                 "arguments": {"invoice_id": "INV-1"}},
            ),
        ]
    )
    runtime, transport = _runtime()
    _engine, events = _drive(reasoner, runtime, _spec())
    # 越级调用被拒：无 L2/L3 披露事件（L1 列表合法），transport 未被调用。
    assert not [
        e for e in events
        if e.event_type == EventType.MCP_DISCLOSED and e.payload["level"] >= 2
    ]
    assert transport.calls == []
    failures = [
        e for e in events
        if e.event_type == EventType.TOOL_CALL_END and e.payload.get("name") == MCP_CALL_TOOL_TOOL
        and "须先 mcp_read_tool_schema" in str(e.payload.get("error") or "")
    ]
    assert failures, "跳过 Schema 的调用必须被拒绝并产生错误事件"


def test_schema_requires_list_first():
    reasoner = _ScriptedReasoner(
        [(MCP_READ_SCHEMA_TOOL, {"server_id": _FINANCE, "tool_name": "get_invoice"})]
    )
    _engine, events = _drive(reasoner, _runtime()[0], _spec())
    assert not [e for e in events if e.event_type == EventType.MCP_DISCLOSED]
    assert any(
        "须先 mcp_list_tools" in str(e.payload.get("error") or "") for e in events
        if e.event_type == EventType.TOOL_CALL_END
    )


def test_unbound_server_rejected():
    reasoner = _ScriptedReasoner([(MCP_LIST_TOOLS_TOOL, {"server_id": _HR})])
    _engine, events = _drive(reasoner, _runtime()[0], _spec())
    assert any(
        "未绑定" in str(e.payload.get("error") or "") for e in events
        if e.event_type == EventType.TOOL_CALL_END
    )


def test_required_mcp_without_runtime_fails_at_compile():
    async def compile_spec():
        await ManagedLangGraphEngine(reasoner=_ScriptedReasoner([])).compile(_spec())

    with pytest.raises(ExecutionEngineError, match="未装配 McpCapabilityRuntime"):
        asyncio.run(compile_spec())


def test_high_risk_server_routes_call_tool_through_approval():
    """风险等级 high → mcp_call_tool 进审批集合；默认 resolver 缺席 → 拒绝。"""
    reasoner = _ScriptedReasoner(
        [
            (MCP_LIST_TOOLS_TOOL, {"server_id": _FINANCE}),
            (MCP_READ_SCHEMA_TOOL, {"server_id": _FINANCE, "tool_name": "pay_invoice"}),
            (
                MCP_CALL_TOOL_TOOL,
                {"server_id": _FINANCE, "tool_name": "pay_invoice",
                 "arguments": {"invoice_id": "INV-1", "amount": 100}},
            ),
        ]
    )
    runtime, transport = _runtime(risk=RiskLevel.HIGH)
    _engine, events = _drive(reasoner, runtime, _spec())
    # 审批拦截：调用未达传输层，approval.requested 携带真实目标工具与参数，
    # Run 进入 awaiting_approval（而非执行）。
    assert transport.calls == []
    requested = [e for e in events if e.event_type == EventType.APPROVAL_REQUESTED]
    assert requested
    detail = requested[0].payload["detail"]
    assert detail["name"] == MCP_CALL_TOOL_TOOL
    assert detail["args"]["tool_name"] == "pay_invoice"
    assert any(
        e.payload.get("reason") == "tool_approval" for e in events
        if e.event_type == EventType.RUN_INTERRUPTED
    )


def test_list_tools_refresh_invalidates_cache():
    runtime, _transport = _runtime()

    async def drive():
        engine = ManagedLangGraphEngine(
            reasoner=_ScriptedReasoner([(MCP_LIST_TOOLS_TOOL, {"server_id": _FINANCE})]),
            mcp_runtime=runtime,
        )
        compiled = await engine.compile(_spec())
        handle = await engine.start(
            StartRequest(agent_id="a", user_id="u", session_id="s", input="x",
                         runtime_type="managed-langgraph"),
            compiled,
        )
        _ = [e async for e in engine.stream(handle)]
        await engine.close(handle)

    asyncio.run(drive())
    # 缓存已填充；refresh=true 失效后重新拉取。
    assert runtime._tools_cache.get(_FINANCE)
    runtime.invalidate_tools(_FINANCE)
    assert runtime._tools_cache.get(_FINANCE) is None


def test_cross_process_approval_resume_preserves_cursors(tmp_path):
    """P0.1：披露游标随图状态进 SQLite Checkpoint——跨进程 attach+resume 后，
    已读过的 Schema 不必重读（游标丢失则 L3 会被"须先读 Schema"拒绝）。"""
    import contextlib

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ksadk.runtime import ResumePayload, ResumeTarget

    db_path = str(tmp_path / "mcp-cursors.db")
    spec = _spec()
    calls = [
        (MCP_LIST_TOOLS_TOOL, {"server_id": _FINANCE}),
        (MCP_READ_SCHEMA_TOOL, {"server_id": _FINANCE, "tool_name": "pay_invoice"}),
        (MCP_CALL_TOOL_TOOL, {"server_id": _FINANCE, "tool_name": "pay_invoice",
                               "arguments": {"invoice_id": "INV-9", "amount": 5}}),
    ]

    def runtime(transport):
        rt = McpCapabilityRuntime()
        rt.bind(
            McpServerBinding(
                descriptor=CapabilityDescriptor(
                    id=_FINANCE, kind="mcp", name="财务工具", description="发票",
                    version="1.0.0", risk_level=RiskLevel.HIGH,
                ),
                transport=transport, required=True,
            )
        )
        return rt

    async def phase_one():
        cm = AsyncSqliteSaver.from_conn_string(db_path)
        saver = await cm.__aenter__()
        try:
            transport = _FakeTransport(_TOOLS)
            rt = runtime(transport)
            engine = ManagedLangGraphEngine(
                reasoner=_ScriptedReasoner(calls), checkpointer=saver, mcp_runtime=rt
            )
            compiled = await engine.compile(spec)
            handle = await engine.start(
                StartRequest(agent_id="a", user_id="u", session_id="s", input="x",
                             runtime_type="managed-langgraph"),
                compiled,
            )
            events = [e async for e in engine.stream(handle)]
            return handle, events, transport
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    async def phase_two(handle, transport):
        cm = AsyncSqliteSaver.from_conn_string(db_path)
        saver = await cm.__aenter__()
        try:
            engine = ManagedLangGraphEngine(
                # 恢复后 reason 直接给最终文本（无需再次披露）。
                reasoner=_ScriptedReasoner([]),
                checkpointer=saver,
                mcp_runtime=runtime(transport),
            )
            compiled = await engine.compile(spec)
            attached = await engine.attach(handle, compiled)
            await engine.resume(
                attached,
                ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
                ResumePayload(kind="approval_decision", call_id="c3", data="approved"),
            )
            events = [e async for e in engine.stream(attached)]
            return events, transport
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    handle, first, transport = asyncio.run(phase_one())
    assert any(e.event_type == EventType.RUN_INTERRUPTED for e in first)
    assert transport.calls == []  # 审批前未执行

    second, transport = asyncio.run(phase_two(handle, transport))
    # 审批通过后调用直达传输层——游标跨进程存活（否则会被"须先读 Schema"拒绝）。
    assert transport.calls == [("pay_invoice", {"invoice_id": "INV-9", "amount": 5})]
    assert second[-1].event_type == EventType.RUN_COMPLETED


def test_mixed_risk_servers_dynamic_approval():
    """P0.1：高低风险 Server 混用——低风险调用直通，高风险调用才进审批。"""
    from ksadk.harness.mcp_runtime import McpCapabilityRuntime

    runtime = McpCapabilityRuntime()
    low_transport = _FakeTransport(_TOOLS)
    high_transport = _FakeTransport(_TOOLS)
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=_LOW, kind="mcp", name="低风险站", description="低风险工具",
                version="1.0.0", risk_level=RiskLevel.LOW,
            ),
            transport=low_transport, required=False,
        )
    )
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=_FINANCE, kind="mcp", name="高风险站", description="高风险工具",
                version="1.0.0", risk_level=RiskLevel.HIGH,
            ),
            transport=high_transport, required=False,
        )
    )
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://mix@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="你是财务助手。"),
        capabilities=CapabilityBindings(
            mcp_bindings=(
                CapabilityBinding(capability_ref=_LOW, required=False, load_policy="on_demand"),
                CapabilityBinding(capability_ref=_FINANCE, required=False, load_policy="on_demand"),
            )
        ),
    )
    reasoner = _ScriptedReasoner(
        [
            # 低风险 Server：全链路（L1→L2→L3）无需审批，直通执行。
            (MCP_LIST_TOOLS_TOOL, {"server_id": _LOW}),
            (MCP_READ_SCHEMA_TOOL, {"server_id": _LOW, "tool_name": "get_invoice"}),
            (MCP_CALL_TOOL_TOOL, {"server_id": _LOW, "tool_name": "get_invoice",
                                   "arguments": {"invoice_id": "INV-L"}}),
            # 高风险 Server：调用触发审批中断。
            (MCP_LIST_TOOLS_TOOL, {"server_id": _FINANCE}),
            (MCP_READ_SCHEMA_TOOL, {"server_id": _FINANCE, "tool_name": "pay_invoice"}),
            (MCP_CALL_TOOL_TOOL, {"server_id": _FINANCE, "tool_name": "pay_invoice",
                                   "arguments": {"invoice_id": "INV-H", "amount": 1}}),
        ]
    )

    async def drive():
        engine = ManagedLangGraphEngine(reasoner=reasoner, mcp_runtime=runtime)
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(agent_id="a", user_id="u", session_id="s", input="x",
                         runtime_type="managed-langgraph"),
            compiled,
        )
        events = [e async for e in engine.stream(handle)]
        return handle, events

    handle, events = asyncio.run(drive())
    # 低风险调用已直通执行；高风险调用未执行，Run 中断等审批。
    assert low_transport.calls == [("get_invoice", {"invoice_id": "INV-L"})]
    assert high_transport.calls == []
    requested = [e for e in events if e.event_type == EventType.APPROVAL_REQUESTED]
    assert requested
    assert requested[0].payload["detail"]["args"]["server_id"] == _FINANCE
    assert any(
        e.payload.get("reason") == "tool_approval" for e in events
        if e.event_type == EventType.RUN_INTERRUPTED
    )
    del handle


def test_idempotent_transport_receives_stable_loop_identity_without_schema_injection():
    transport = _IdempotentTransport(_TOOLS)
    runtime = McpCapabilityRuntime()
    runtime.bind(
        McpServerBinding(
            descriptor=_descriptor(),
            transport=transport,
            required=True,
            idempotency_mode="transport",
        )
    )
    reasoner = _ScriptedReasoner(
        [
            (MCP_LIST_TOOLS_TOOL, {"server_id": _FINANCE}),
            (
                MCP_READ_SCHEMA_TOOL,
                {"server_id": _FINANCE, "tool_name": "pay_invoice"},
            ),
            (
                MCP_CALL_TOOL_TOOL,
                {
                    "server_id": _FINANCE,
                    "tool_name": "pay_invoice",
                    "arguments": {"invoice_id": "INV-IDEMP", "amount": 7},
                },
            ),
        ]
    )

    _, events = _drive(reasoner, runtime, _spec())

    assert events[-1].event_type == EventType.RUN_COMPLETED
    assert transport.calls == [("pay_invoice", {"invoice_id": "INV-IDEMP", "amount": 7})]
    assert len(transport.contexts) == 1
    context = transport.contexts[0]
    assert context.call_id == "c3"
    assert context.idempotency_key == McpToolCallContext.create(
        invocation_id=context.invocation_id,
        call_id="c3",
    ).idempotency_key
    assert "idempotency_key" not in transport.calls[0][1]


def test_transport_idempotency_declaration_requires_contextual_transport():
    runtime = McpCapabilityRuntime()
    with pytest.raises(McpRuntimeError, match="call_tool_with_context"):
        runtime.bind(
            McpServerBinding(
                descriptor=_descriptor(),
                transport=_FakeTransport(_TOOLS),
                idempotency_mode="transport",
            )
        )
