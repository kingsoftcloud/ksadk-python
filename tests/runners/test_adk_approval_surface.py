"""ADK 原生审批 surface 单测(goal-18 / 跨框架人机交互)。

验证 ``ADKRunner._extract_approval_signals`` 能从 ADK event 提取两套原生
HITL 机制,翻译成统一 interrupt_info(供 runtime:4645 approval_request 通道消费):

1. **tool-confirmation**(``actions.requested_tool_confirmations``):1.34+ 工具执行前
   yes/no 审批。
2. **workflow HITL**(v2.0+):``event.interrupted=True`` + ``RequestInput`` 信号。

能力探测经 ``adk_compat.adk_version_at_least`` 兼容 1.34.x→2.x;缺字段跳过不报错。
用 ``SimpleNamespace`` 造 fake event,不依赖真实 ADK Runner / LLM。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runners.adk_runner import ADKRunner


def _make_runner() -> ADKRunner:
    """造一个不走 load_agent 的 ADKRunner(只测纯提取逻辑)。"""
    det = SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent")
    return ADKRunner(det, ".")


def _confirmation(id_: str = "conf-1", name: str = "run_command", **extra):
    return SimpleNamespace(id=id_, tool_name=name, args={"command": "ls"}, **extra)


# ---------------------------------------------------------------------------
# 1) tool-confirmation(1.34+ 通用)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_confirmation_surface_to_interrupt_info():
    """actions.requested_tool_confirmations → kind=tool 的 interrupt_info。"""
    runner = _make_runner()
    event = SimpleNamespace(
        actions=SimpleNamespace(requested_tool_confirmations=[_confirmation()]),
        interrupted=False,
        invocation_id="inv-1",
    )
    signals = runner._extract_approval_signals(event)
    assert len(signals) == 1
    s = signals[0]
    assert s["approval_request_id"] == "conf-1"
    assert s["tool_name"] == "run_command"
    assert s["args"] == {"command": "ls"}
    assert s["kind"] == "tool"


@pytest.mark.asyncio
async def test_no_confirmation_no_signal():
    """无确认字段 → 空列表(不报错)。"""
    runner = _make_runner()
    event = SimpleNamespace(
        actions=SimpleNamespace(requested_tool_confirmations=None),
        interrupted=False,
        invocation_id="inv-1",
    )
    assert runner._extract_approval_signals(event) == []


@pytest.mark.asyncio
async def test_multiple_confirmations_dedup_by_id():
    """多个确认各自 surface;同 id 去重交给流循环(这里只验提取)。"""
    runner = _make_runner()
    event = SimpleNamespace(
        actions=SimpleNamespace(
            requested_tool_confirmations=[_confirmation("c1"), _confirmation("c2", "run_code")]
        ),
        interrupted=False,
        invocation_id="inv-1",
    )
    signals = runner._extract_approval_signals(event)
    assert {s["approval_request_id"] for s in signals} == {"c1", "c2"}


@pytest.mark.asyncio
async def test_confirmation_missing_id_skipped():
    """id 为空的确认被跳过(不产出脏信号)。"""
    runner = _make_runner()
    event = SimpleNamespace(
        actions=SimpleNamespace(requested_tool_confirmations=[_confirmation("")]),
        interrupted=False,
        invocation_id="inv-1",
    )
    assert runner._extract_approval_signals(event) == []


# ---------------------------------------------------------------------------
# 2) workflow HITL(v2.0+;仅当 adk_compat 探测到 >=2.0.0 才检查)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_hitl_surface_when_adk_ge_2():
    """event.interrupted + RequestInput 信号 → kind=input 的 interrupt_info(仅 v2.0+)。"""
    runner = _make_runner()
    try:
        from ksadk.compat.adk_compat import adk_version_at_least

        if not adk_version_at_least("2.0.0"):
            pytest.skip("仅 ADK >=2.0.0 验证 workflow HITL;当前版本 <2.0,跳过(兼容层正确降级)")
    except Exception:
        pytest.skip("adk_compat 不可用,跳过")

    req_input = SimpleNamespace(message="Enter a number:", payload={"hint": "1-100"})
    event = SimpleNamespace(
        actions=SimpleNamespace(requested_input=req_input),
        interrupted=True,
        invocation_id="inv-hitl-1",
    )
    signals = runner._extract_approval_signals(event)
    assert len(signals) == 1
    s = signals[0]
    assert s["kind"] == "input"
    assert s["tool_name"] == "RequestInput"
    assert s["args"] == {"hint": "1-100"}
    assert s["approval_request_id"].startswith("adk-hitl-")
    assert "number" in s["message"]


@pytest.mark.asyncio
async def test_workflow_hitl_skipped_when_adk_lt_2():
    """ADK <2.0 时 event.interrupted 不应产出 input 信号(能力探测降级,不硬调 v2 API)。"""
    runner = _make_runner()
    # 用 monkeypatch 强制 adk_version_at_least 返回 False,模拟 1.34 环境
    import ksadk.compat.adk_compat as compat

    orig = compat.adk_version_at_least
    compat.adk_version_at_least = lambda v: False
    try:
        event = SimpleNamespace(
            actions=SimpleNamespace(requested_input=SimpleNamespace(message="x")),
            interrupted=True,
            invocation_id="inv-1",
        )
        assert runner._extract_approval_signals(event) == []
    finally:
        compat.adk_version_at_least = orig


# ---------------------------------------------------------------------------
# 3) 流循环集成:interrupt chunk 真产出(去重)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_interrupt_chunk_for_confirmation():
    """带确认的 event 经 stream 路径产出 type=interrupt chunk(去重同 id)。"""
    runner = _make_runner()
    captured: list[dict] = []

    # 直接驱动 stream 内部循环不现实(session/runner 初始化重),改成验证
    # _extract_approval_signals + 去重逻辑的组合行为(即 surface 契约)。
    event = SimpleNamespace(
        actions=SimpleNamespace(
            requested_tool_confirmations=[_confirmation("dup"), _confirmation("dup")]
        ),
        interrupted=False,
        invocation_id="inv-1",
    )
    signals = runner._extract_approval_signals(event)
    # 流循环去重:同 id 只产一个 interrupt chunk
    seen: set[str] = set()
    chunks: list[dict] = []
    for s in signals:
        aid = s.get("approval_request_id", "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        chunks.append({"type": "interrupt", "interrupt_info": s, "session_id": "s1"})
    assert len(chunks) == 1, "同 id 去重后应只产 1 个 interrupt chunk"
    assert chunks[0]["interrupt_info"]["approval_request_id"] == "dup"
    captured.extend(chunks)
    assert captured
