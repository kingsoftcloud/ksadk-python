"""PR D2：WorkingState 数据模型 + 确定性提取 + 门控重注入。

门控语义：仅 ``prompt_integration_mode=="ksadk_hosted"`` 时压缩产出 WorkingState 并在
下一轮 runner payload 重注入。非门控：不写 checkpoint working_state 键、零注入。
确定性优先：pending_tools/approvals/active_files/current_goal 来自事实事件，不靠摘要模型猜。
content_hash 稳定。Prompt 明文不进 Trace（working_state 不进 shadow plan/trace）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.conversations.runtime_input import _build_runner_request_payload, _render_working_state_xml
from ksadk.conversations.runtime_payloads import PreparedConversationTurn
from ksadk.conversations.semantic_summary import WorkingState, extract_working_state
from ksadk.runtime_context import PlatformInvocationContext
from ksadk.sessions.base import SessionEvent


# --- WorkingState 数据模型 + 提取 ---


def _tool_call_event(seq: int, tool_name: str, path: str = "", inv: str = "i1") -> SessionEvent:
    return SessionEvent(
        id=f"tc-{seq}",
        seq_id=seq,
        event_type="tool_call",
        author="runner",
        invocation_id=inv,
        content={"role": "model", "parts": [{"text": tool_name}]},
        metadata={"tool_name": tool_name, "tool_args": {"path": path} if path else {}},
    )


def _approval_event(seq: int, text: str, inv: str = "i1") -> SessionEvent:
    return SessionEvent(
        id=f"ar-{seq}",
        seq_id=seq,
        event_type="approval_request",
        author="runner",
        invocation_id=inv,
        content={"role": "model", "parts": [{"text": text}]},
    )


def _user_event(seq: int, text: str, inv: str = "i1") -> SessionEvent:
    return SessionEvent(
        id=f"u-{seq}",
        seq_id=seq,
        event_type="user_message",
        author="user",
        invocation_id=inv,
        content={"role": "user", "parts": [{"text": text}]},
    )


def test_extract_deterministic_from_facts() -> None:
    """pending_tools/approvals/active_files/current_goal 来自事实事件。"""
    events = [
        _user_event(1, "升级到 Python 3.12"),
        _tool_call_event(2, "read_workspace_file", "setup.py"),
        _approval_event(3, "approve write notes.txt"),
    ]
    pinned = {
        "current_user_goal": "升级到 Python 3.12",
        "pending_approvals": ["approve write notes.txt"],
        "pending_tools": [],
        "attachment_refs": [],
    }
    ws = extract_working_state(events, pinned_state=pinned, source_seq_range=(1, 3))
    assert ws.current_goal == "升级到 Python 3.12"
    assert ws.active_files == [{"path": "setup.py", "tool_name": "read_workspace_file"}]
    assert ws.pending_approvals == [{"text": "approve write notes.txt"}]
    assert ws.source_seq_range == (1, 3)


def test_content_hash_stable() -> None:
    """相同事件序列两次提取 hash 一致。"""
    events = [_user_event(1, "g"), _tool_call_event(2, "t", "p.py")]
    pinned = {"current_user_goal": "g", "pending_approvals": [], "pending_tools": [], "attachment_refs": []}
    ws1 = extract_working_state(events, pinned_state=pinned, source_seq_range=(1, 2))
    ws2 = extract_working_state(events, pinned_state=pinned, source_seq_range=(1, 2))
    assert ws1.content_hash() == ws2.content_hash()
    assert ws1.content_hash().startswith("sha256:")


def test_to_audit_dict_no_plaintext_prompt() -> None:
    """审计 dict 含结构化字段 + content_hash/status，不含 prompt 明文。"""
    ws = WorkingState(current_goal="g", active_files=[{"path": "a.py"}])
    d = ws.to_audit_dict()
    assert d["current_goal"] == "g"
    assert d["content_hash"].startswith("sha256:")
    assert d["status"] == "succeeded"
    assert d["schema_version"] == "v1"


def test_render_working_state_xml() -> None:
    """渲染成 <working_state> XML 段，含 goal/files/pending。"""
    xml = _render_working_state_xml(
        {
            "current_goal": "升级到 3.12",
            "next_action": "跑测试",
            "active_files": [{"path": "setup.py"}],
            "pending_tools": [{"text": "run_tests"}],
            "pending_approvals": [{"text": "approve write"}],
        }
    )
    assert xml.startswith("<working_state>")
    assert xml.endswith("</working_state>")
    assert "当前目标：升级到 3.12" in xml
    assert "活跃文件：setup.py" in xml
    assert "未完成工具：run_tests" in xml
    assert "待审批：approve write" in xml


def test_render_empty_working_state_returns_empty() -> None:
    assert _render_working_state_xml({}) == ""


# --- 门控重注入 ---


def _ctx(session_id: str) -> PlatformInvocationContext:
    return PlatformInvocationContext(
        agent_id="a",
        user_id="u",
        account_id="",
        session_id=session_id,
        history=[],
        input_content=[],
        input_messages=[],
        input_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        runner_type="langgraph",
    )


def _prepared(*, mode: str, working_state: dict | None, instructions: str = "本轮指令") -> PreparedConversationTurn:
    return PreparedConversationTurn(
        session_id="sess-ws",
        invocation_id="inv-1",
        user_input="问题",
        user_display_input="问题",
        history=[],
        input_content=[],
        input_messages=[],
        user_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        model_metadata={},
        model_options={},
        instructions=instructions,
        request_metadata={},
        compaction_triggered=False,
        compaction_trigger=None,
        compacted_until_seq_id=None,
        resume_input=None,
        run_mode="foreground",
        run_trigger="new_run",
        request_history=[],
        request_responses_history=[],
        responses_history=[],
        shadow_context_plan=None,
        compiled_prompt=None,
        prompt_integration_mode=mode,
        working_state=working_state,
    )


def test_ksadk_hosted_injects_working_state_into_instructions(monkeypatch) -> None:
    prepared = _prepared(
        mode="ksadk_hosted",
        working_state={"current_goal": "升级 3.12", "active_files": [{"path": "setup.py"}]},
    )
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=_ctx(prepared.session_id), runner=None
    )
    assert "<working_state>" in payload["instructions"]
    assert "本轮指令" in payload["instructions"]
    assert "升级 3.12" in payload["instructions"]


def test_framework_mode_no_injection() -> None:
    """非门控 + working_state 存在 → 仍零注入（payload instructions 不含 working_state）。"""
    prepared = _prepared(
        mode="",
        working_state={"current_goal": "升级 3.12"},
    )
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=_ctx(prepared.session_id), runner=None
    )
    assert payload["instructions"] == "本轮指令"
    assert "working_state" not in payload["instructions"]


def test_ksadk_hosted_no_working_state_no_injection() -> None:
    """ksadk_hosted 但 working_state=None（无 checkpoint）→ 零注入。"""
    prepared = _prepared(mode="ksadk_hosted", working_state=None)
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=_ctx(prepared.session_id), runner=None
    )
    assert payload["instructions"] == "本轮指令"


def test_ksadk_hosted_empty_working_state_no_injection() -> None:
    """ksadk_hosted 但 working_state 字段全空 → 渲染空 → 零注入。"""
    prepared = _prepared(
        mode="ksadk_hosted",
        working_state={"current_goal": "", "active_files": [], "pending_tools": []},
    )
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=_ctx(prepared.session_id), runner=None
    )
    assert payload["instructions"] == "本轮指令"


# --- 集成：compact_conversation_history 写 working_state + build_run_input 重注入 ---


@pytest.mark.asyncio
async def test_compact_writes_working_state_for_ksadk_hosted(monkeypatch) -> None:
    """ksadk_hosted 压缩 → checkpoint metadata 含 working_state.content_hash/source_seq_range；
    非 ksadk_hosted → 不含该键。"""
    from ksadk.conversations.runtime_compaction import compact_conversation_history
    from ksadk.sessions.in_memory import InMemorySessionService

    md = {"context_window_tokens": 200_000, "limits": {"max_output_tokens": 32_000}}
    events = []
    for i in range(6):
        events.extend(
            [
                SessionEvent(
                    id=f"u{i}",
                    seq_id=i * 2 + 1,
                    event_type="user_message",
                    author="user",
                    invocation_id=f"pre-{i}",
                    content={"role": "user", "parts": [{"text": "u" * 60_000}]},
                ),
                SessionEvent(
                    id=f"a{i}",
                    seq_id=i * 2 + 2,
                    event_type="assistant_message",
                    author="runner",
                    invocation_id=f"pre-{i}",
                    content={"role": "model", "parts": [{"text": "a" * 60_000}]},
                ),
            ]
        )

    # ksadk_hosted
    service_h = InMemorySessionService()
    await service_h.create_session(agent_id="a", user_id="u", session_id="sess-ws-h")
    for ev in events:
        await service_h.append_event("sess-ws-h", ev)
    cp_h = await compact_conversation_history(
        session_id="sess-ws-h",
        author="a",
        model="m",
        model_metadata=md,
        session_service_provider=lambda: service_h,
        prompt_integration_mode="ksadk_hosted",
    )
    assert cp_h is not None
    meta_h = cp_h.metadata or {}
    assert "working_state" in meta_h
    assert meta_h["working_state"]["content_hash"].startswith("sha256:")
    assert meta_h["working_state"]["source_seq_range"] == [1, 4]
    assert meta_h["working_state"]["status"] == "succeeded"

    # 非 ksadk_hosted
    service_f = InMemorySessionService()
    await service_f.create_session(agent_id="a", user_id="u", session_id="sess-ws-f")
    for ev in events:
        await service_f.append_event("sess-ws-f", ev)
    cp_f = await compact_conversation_history(
        session_id="sess-ws-f",
        author="a",
        model="m",
        model_metadata=md,
        session_service_provider=lambda: service_f,
        prompt_integration_mode="",
    )
    assert cp_f is not None
    meta_f = cp_f.metadata or {}
    assert "working_state" not in meta_f
