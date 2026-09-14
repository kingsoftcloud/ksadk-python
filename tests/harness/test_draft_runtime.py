"""P2/P2.1「保存即可试用」Draft Runtime 调试通路测试。"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ksadk.harness.capabilities import CapabilityDescriptor, RiskLevel
from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.draft_runtime import (
    DRAFT_REF_PREFIX,
    DRAFT_SESSION_PREFIX,
    DraftRuntime,
    DraftRuntimeError,
)
from ksadk.harness.events import EventType
from ksadk.harness.mcp_runtime import (
    McpCapabilityRuntime,
    McpServerBinding,
    McpTransport,
)
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import HarnessSpec


class _EchoReasoner:
    """无工具模型：直接回一句测试文本（可记录看到的上下文）。"""

    def __init__(self, final_text: str = "草稿测试回复。") -> None:
        self.final_text = final_text
        self.seen_messages: list[tuple[dict, ...]] = []

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, tools
        self.seen_messages.append(tuple(messages))
        return HarnessReasoningTurn(final_text=self.final_text)


def _draft_payload() -> dict:
    return {
        "role": {"name": "finance-analyst-draft", "objective": "财务分析（草稿）"},
        "model": {"profileRef": "model-profile://kimi-k3@1.0.0"},
    }


def _runtime(reasoner, **kwargs) -> DraftRuntime:
    return DraftRuntime(reasoner=reasoner, **kwargs)


@pytest.mark.asyncio
async def test_draft_compiles_and_converses_immediately():
    runtime = _runtime(_EchoReasoner())
    session = await runtime.compile(_draft_payload())
    assert session.draft_ref.startswith(DRAFT_REF_PREFIX)
    assert session._compiled is not None, "保存时已完成 Runtime 编译"
    events = await session.converse("测试一下这个草稿")
    final = [
        e for e in events
        if e.event_type == EventType.TEXT_COMPLETED and e.phase == "final_answer"
    ]
    assert final and final[0].payload["text"] == "草稿测试回复。"
    assert events[-1].event_type == EventType.RUN_COMPLETED
    await runtime.close()


@pytest.mark.asyncio
async def test_multi_turn_conversation_shares_context():
    """多轮共享上下文：第二轮能看到第一轮的用户输入与模型回复。"""
    reasoner = _EchoReasoner()
    runtime = _runtime(reasoner)
    session = await runtime.compile(_draft_payload())
    await session.converse("我的预算是 100 万")
    await session.converse("那实际是 120 万，偏差率多少")
    # 同一稳定 session（不是逐轮新建 draft session）。
    started = [
        e for e in (await session.converse("重复一遍我的预算数字"))
        if e.event_type == EventType.RUN_STARTED
    ]
    assert started and started[0].session_id.startswith(DRAFT_SESSION_PREFIX)
    # 第三轮的模型上下文含第一、二轮内容（跨轮历史注入生效）。
    rendered = "\n".join(
        str(m.get("content")) for msgs in reasoner.seen_messages for m in msgs
    )
    assert "预算是 100 万" in rendered
    assert "草稿测试回复" in rendered  # 第一轮模型回复进入历史
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_validation_fails_at_save_time():
    """保存时即完成全部 Runtime 校验（工具名冲突等），不拖到首次发消息。"""
    runtime = _runtime(
        _EchoReasoner(),
        tools={"mcp_list_tools": object()},  # 与披露工具名冲突
    )
    with pytest.raises(DraftRuntimeError, match="runtime 校验失败"):
        await runtime.compile(_draft_payload())


def test_invalid_draft_fails_at_compile():
    import asyncio

    runtime = _runtime(_EchoReasoner())
    with pytest.raises(DraftRuntimeError, match="draft 编译失败"):
        asyncio.run(runtime.compile({"model": {"profileRef": "not-a-ref"}}))


# ---------------------------------------------------------------- MCP 审批


_MCP_TOOLS = {
    "pay_invoice": {
        "name": "pay_invoice",
        "description": "支付发票（高风险写操作）",
        "inputSchema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
        },
    },
}


class _PayTransport(McpTransport):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[dict]:
        return list(_MCP_TOOLS.values())

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments)))
        return {"status": "ok"}


class _CallToolReasoner:
    """第一轮调用高风险 MCP 工具；resume 后给最终文本。"""

    def __init__(self) -> None:
        self.step = 0

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, messages
        if self.step == 0:
            self.step += 1
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="c1",
                        name="mcp_list_tools",
                        arguments={"server_id": "mcp://pay-server@1.0.0"},
                    ),
                )
            )
        if self.step == 1:
            self.step += 1
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="c2",
                        name="mcp_read_tool_schema",
                        arguments={
                            "server_id": "mcp://pay-server@1.0.0",
                            "tool_name": "pay_invoice",
                        },
                    ),
                )
            )
        if self.step == 2:
            self.step += 1
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="c3",
                        name="mcp_call_tool",
                        arguments={
                            "server_id": "mcp://pay-server@1.0.0",
                            "tool_name": "pay_invoice",
                            "arguments": {"invoice_id": "INV-1"},
                        },
                    ),
                )
            )
        return HarnessReasoningTurn(final_text="已支付。")


class _CallTwoToolsReasoner(_CallToolReasoner):
    """同一 Run 连续调用两个高风险工具，验证审批可连续挂起。"""

    async def complete(self, *, model, prompt, messages, tools):
        if self.step <= 2:
            return await super().complete(
                model=model,
                prompt=prompt,
                messages=messages,
                tools=tools,
            )
        if self.step == 3:
            self.step += 1
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="c4",
                        name="mcp_call_tool",
                        arguments={
                            "server_id": "mcp://pay-server@1.0.0",
                            "tool_name": "pay_invoice",
                            "arguments": {"invoice_id": "INV-2"},
                        },
                    ),
                )
            )
        return HarnessReasoningTurn(final_text="两张发票均已支付。")


def _mcp_runtime(transport: McpTransport) -> McpCapabilityRuntime:
    runtime = McpCapabilityRuntime()
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id="mcp://pay-server@1.0.0", kind="mcp", name="支付",
                description="支付工具", version="1.0.0",
                risk_level=RiskLevel.HIGH,
            ),
            transport=transport, required=True,
        )
    )
    return runtime


def _mcp_payload() -> dict:
    return {
        "role": {"name": "pay-draft", "objective": "支付草稿"},
        "model": {"profileRef": "model-profile://test-model@1.0.0"},
        "capabilities": {
            "mcpBindings": [
                {"bindingRef": "mcp://pay-server@1.0.0"}
            ]
        },
    }


@pytest.mark.asyncio
async def test_approval_interrupt_and_resume_inside_draft():
    """草稿内审批照常工作：高风险调用中断 → approve → 续跑完成。"""
    transport = _PayTransport()
    mcp = _mcp_runtime(transport)
    runtime = DraftRuntime(
        reasoner=_CallToolReasoner(), mcp_runtime=mcp, checkpointer=MemorySaver()
    )
    session = await runtime.compile(_mcp_payload())
    events = await session.converse("支付发票 INV-1")
    # 中断在审批，工具未执行。
    assert session.awaiting_approval
    assert transport.calls == []
    assert any(e.event_type == EventType.APPROVAL_REQUESTED for e in events)
    assert any(
        e.payload.get("reason") == "tool_approval" for e in events
        if e.event_type == EventType.RUN_INTERRUPTED
    )
    # 有挂起审批时不得开新轮次。
    with pytest.raises(DraftRuntimeError, match="挂起审批"):
        await session.converse("先聊别的")
    # 批准 → 续跑 → 真实调用到达传输层。
    resumed = await session.approve()
    assert transport.calls == [("pay_invoice", {"invoice_id": "INV-1"})]
    assert resumed[-1].event_type == EventType.RUN_COMPLETED
    assert not session.awaiting_approval
    # 审批完成后可继续多轮。
    await session.converse("再确认一下")
    await runtime.close()


@pytest.mark.asyncio
async def test_approval_reject_keeps_run_alive():
    """reject：工具被拒（未执行），Run 正常收尾。"""
    transport = _PayTransport()
    mcp = _mcp_runtime(transport)
    runtime = DraftRuntime(
        reasoner=_CallToolReasoner(), mcp_runtime=mcp, checkpointer=MemorySaver()
    )
    session = await runtime.compile(_mcp_payload())
    await session.converse("支付发票 INV-1")
    resumed = await session.reject()
    assert transport.calls == []  # 拒绝：调用未达传输层
    assert resumed[-1].event_type == EventType.RUN_COMPLETED
    await runtime.close()


@pytest.mark.asyncio
async def test_two_approvals_in_same_run_are_preserved_in_sequence():
    """批准第一次调用后若再次中断，Handle 必须继续保留而非提前关闭。"""
    transport = _PayTransport()
    runtime = DraftRuntime(
        reasoner=_CallTwoToolsReasoner(),
        mcp_runtime=_mcp_runtime(transport),
        checkpointer=MemorySaver(),
    )
    session = await runtime.compile(_mcp_payload())

    await session.converse("依次支付 INV-1 和 INV-2")
    assert session.awaiting_approval
    first_resume = await session.approve()
    assert transport.calls == [("pay_invoice", {"invoice_id": "INV-1"})]
    assert session.awaiting_approval
    assert any(e.event_type == EventType.APPROVAL_REQUESTED for e in first_resume)

    second_resume = await session.approve()
    assert transport.calls == [
        ("pay_invoice", {"invoice_id": "INV-1"}),
        ("pay_invoice", {"invoice_id": "INV-2"}),
    ]
    assert second_resume[-1].event_type == EventType.RUN_COMPLETED
    assert not session.awaiting_approval
    await runtime.close()


@pytest.mark.asyncio
async def test_resume_failure_keeps_pending_approval_for_retry(monkeypatch):
    """引擎恢复失败不能吞掉审批入口，用户仍可重试或放弃。"""
    runtime = DraftRuntime(
        reasoner=_CallToolReasoner(),
        mcp_runtime=_mcp_runtime(_PayTransport()),
        checkpointer=MemorySaver(),
    )
    session = await runtime.compile(_mcp_payload())
    await session.converse("支付发票 INV-1")
    pending = session.pending

    async def _fail_resume(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("temporary checkpoint failure")

    monkeypatch.setattr(session.engine, "resume", _fail_resume)
    with pytest.raises(RuntimeError, match="temporary checkpoint failure"):
        await session.approve()
    assert session.pending is pending
    assert session.awaiting_approval
    await session.discard_approval()
    await runtime.close()


# ------------------------------------------------------------- 生命周期隔离


@pytest.mark.asyncio
async def test_draft_isolated_from_formal_lifecycle():
    from ksadk.harness.lifecycle import LocalLifecycleManager

    manager = LocalLifecycleManager()
    runtime = _runtime(_EchoReasoner())
    session = await runtime.compile(_draft_payload())
    await session.converse("测试对话")
    assert not manager.registry._routes  # 正式路径零感知
    assert session.spec.agent_revision_ref.startswith(DRAFT_REF_PREFIX)
    started_session = session._session_id
    assert started_session.startswith(DRAFT_SESSION_PREFIX)
    await runtime.close()


@pytest.mark.asyncio
async def test_draft_build_parity_shares_compiler_path():
    from ksadk.harness.lifecycle import BuildPipeline

    runtime = _runtime(_EchoReasoner())
    session = await runtime.compile(_draft_payload())
    manifest = BuildPipeline().build(
        revision_payload=dict(_draft_payload()), revision_ref="agent-revision://proj-1@1"
    )
    assert manifest.build_id.startswith("bld_")
    assert isinstance(session.spec, HarnessSpec)
    await runtime.close()


@pytest.mark.asyncio
async def test_unknown_draft_session_rejected():
    runtime = _runtime(_EchoReasoner())
    with pytest.raises(DraftRuntimeError, match="unknown draft"):
        await runtime.session("nope")


@pytest.mark.asyncio
async def test_resave_same_draft_id_replaces_session():
    """同 draft_id 重新保存：旧会话被替换（挂起审批句柄一并释放）。"""
    runtime = _runtime(_EchoReasoner(), checkpointer=MemorySaver())
    old = await runtime.compile(_draft_payload(), draft_id="same")
    await old.converse("第一版")
    assert old.turns == 1
    new = await runtime.compile(_draft_payload(), draft_id="same")
    assert new is not old and new.turns == 0
    assert await runtime.session("same") is new
    # 旧会话历史不泄漏到新会话。
    await new.converse("第二版")
    assert not any("第一版" in str(m) for m in new.history)
    await runtime.close()


@pytest.mark.asyncio
async def test_session_ttl_expiry():
    """TTL 惰性清理：超时会话经 session() 访问时被丢弃。"""
    runtime = DraftRuntime(reasoner=_EchoReasoner(), session_ttl_seconds=0.0)
    session = await runtime.compile(_draft_payload(), draft_id="ttl")
    await session.converse("hi")
    import time

    time.sleep(0.01)
    with pytest.raises(DraftRuntimeError, match="unknown draft"):
        await runtime.session("ttl")


@pytest.mark.asyncio
async def test_session_ttl_expiry_closes_pending_handle():
    """TTL 清理不能只删索引，还必须从 Engine 释放挂起 Run。"""
    runtime = DraftRuntime(
        reasoner=_CallToolReasoner(),
        mcp_runtime=_mcp_runtime(_PayTransport()),
        checkpointer=MemorySaver(),
        session_ttl_seconds=0.0,
    )
    session = await runtime.compile(_mcp_payload(), draft_id="ttl-pending")
    await session.converse("支付发票 INV-1")
    assert session.pending is not None
    run_id = session.pending.handle.run_id

    import asyncio

    await asyncio.sleep(0.01)
    with pytest.raises(DraftRuntimeError, match="unknown draft"):
        await runtime.session("ttl-pending")
    assert not session.awaiting_approval
    assert run_id not in session.engine._runs


@pytest.mark.asyncio
async def test_close_session_releases_pending_approval():
    transport = _PayTransport()
    mcp = _mcp_runtime(transport)
    runtime = DraftRuntime(
        reasoner=_CallToolReasoner(), mcp_runtime=mcp, checkpointer=MemorySaver()
    )
    session = await runtime.compile(_mcp_payload(), draft_id="d1")
    await session.converse("支付发票 INV-1")
    assert session.awaiting_approval
    await runtime.close_session("d1")
    assert not session.awaiting_approval
    with pytest.raises(DraftRuntimeError, match="unknown draft"):
        await runtime.session("d1")


# ------------------------------------------------------------------- Skill


@pytest.mark.asyncio
async def test_draft_with_skill_binding_resolves_and_discloses(tmp_path):
    skill_root = tmp_path / "finance-budget-analysis@1.0.0"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\nname: 预算偏差分析\ndescription: 分析预算与实际支出偏差\n---\n"
        "先读取预算和实际支出。\n",
        encoding="utf-8",
    )
    payload = {
        "role": {"name": "draft-with-skill", "objective": "草稿带技能"},
        "model": {"profileRef": "model-profile://test-model@1.0.0"},
        "capabilities": {
            "skillBindings": [
                {"skillRef": "skill://finance-budget-analysis@1.0.0",
                 "contentHash": "sha256:test-fixture"}
            ]
        },
    }
    runtime = DraftRuntime(reasoner=_EchoReasoner(), local_dir=str(tmp_path))
    session = await runtime.compile(payload)
    assert not session.warnings
    events = await session.converse("测试")
    assert events[-1].event_type == EventType.RUN_COMPLETED
    await runtime.close()


@pytest.mark.asyncio
async def test_context_engine_does_not_duplicate_current_user_input():
    """Draft 历史与 current_input 交汇处只向模型注入一份本轮问题。"""
    reasoner = _EchoReasoner()
    runtime = DraftRuntime(
        reasoner=reasoner,
        context_engine=HarnessContextEngine(),
    )
    session = await runtime.compile(_draft_payload())
    await session.converse("这句话只能出现一次")

    current_turn = reasoner.seen_messages[0]
    exact_matches = [
        message for message in current_turn
        if message.get("role") == "user"
        and message.get("content") == "这句话只能出现一次"
    ]
    assert len(exact_matches) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_oversized_current_input_fails_explicitly_instead_of_being_dropped():
    """单条本轮输入超预算时必须失败，不能退回上一轮或伪装成功。"""
    reasoner = _EchoReasoner()
    runtime = DraftRuntime(
        reasoner=reasoner,
        context_engine=HarnessContextEngine(),
    )
    session = await runtime.compile(_draft_payload())

    oversized = "本轮最新输入不可丢弃。" * 12000
    events = await session.converse(oversized)

    failed = [event for event in events if event.event_type == EventType.RUN_FAILED]
    assert failed
    assert "本轮用户输入不会被静默丢弃" in failed[-1].payload["error"]
    assert "current_input_tokens=" in failed[-1].payload["error"]
    assert not reasoner.seen_messages
    await runtime.close()
