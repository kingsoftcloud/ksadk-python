"""PR C：budget_tool_result_for_event 单项预算 + streaming sink 门控接线。

门控语义：仅 ``enabled=True``（即 ``prompt_integration_mode=="ksadk_hosted"``）时 bound
``content.parts[0].text``（下一轮 history → 模型输入的那条）；``metadata.tool_output`` 始终保留原值。
``enabled=False`` → ``(str(output), {})`` 与旧 ``text=str(output)`` 字节级一致。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.conversations.context import budget_tool_result_for_event
from ksadk.tools.result_budget import ToolResultBudget


def _budget(tmp_path) -> ToolResultBudget:
    return ToolResultBudget(
        max_chars=20,
        preview_chars=8,
        persist_threshold_chars=12,
        persist_dir=tmp_path,
    )


# --- helper 单测 ---


def test_disabled_is_byte_identical_to_legacy_string(monkeypatch, tmp_path) -> None:
    """enabled=False → (str(output), {}) 与旧 text=str(output) 字节级一致（dict/大串/已预算 dict）。"""
    monkeypatch.delenv("KSADK_TOOL_RESULT_DIR", raising=False)
    for output in ("短串", {"ok": True, "content": "x"}, {"persisted": {"path": "/p"}}):
        text, extras = budget_tool_result_for_event(
            tool_name="t", tool_output=output, tool_call_id="c", enabled=False
        )
        assert text == str(output)
        assert extras == {}


def test_enabled_small_output_no_persist(tmp_path) -> None:
    """enabled=True + 小串 → (rendered, {})，无落盘、无 extras。"""
    text, extras = budget_tool_result_for_event(
        tool_name="t",
        tool_output="短串",
        tool_call_id="c",
        enabled=True,
        budget=_budget(tmp_path),
    )
    assert text == "短串"
    assert extras == {}
    assert list(tmp_path.iterdir()) == []


def test_enabled_oversized_string_persists_and_bounds(tmp_path) -> None:
    """enabled=True + 超阈值大串 → text 含 preview + persisted-suffix，extras 有 truncated + 真实落盘文件。"""
    big = "x" * 200
    text, extras = budget_tool_result_for_event(
        tool_name="t",
        tool_output=big,
        tool_call_id="call_42",
        enabled=True,
        budget=_budget(tmp_path),
    )
    audit = extras["tool_result_budget"]
    assert audit["truncated"] is True
    assert audit["original_chars"] == 200
    assert audit["persisted"]["path"]
    persisted_path = audit["persisted"]["path"]
    # 落盘文件含全文
    assert open(persisted_path, encoding="utf-8").read() == big
    # text 被 bound（远小于 200）且含 persisted 引用
    assert len(text) < 200
    assert "[persisted-output]" in text
    assert persisted_path in text


def test_enabled_already_budgeted_dict_does_not_repersist(monkeypatch, tmp_path) -> None:
    """已预算 dict（workspace toolset 真实形状，带顶层 persisted）→ _stringify_part_text 干净渲染 < 阈值，不重复落盘。"""
    monkeypatch.setenv("KSADK_TOOL_RESULT_DIR", str(tmp_path))
    # workspace.read_workspace_file 真实返回形状：preview 字段 + 顶层 persisted（budget_tool_output 产出）
    already = {
        "ok": True,
        "content": "preview-text",
        "persisted": {"path": "/existing/path", "mime_type": "text/plain"},
        "truncated": True,
        "original_chars": 99999,
        "preview_chars": 12,
    }
    # 用默认 50k 预算（真实场景）：渲染后 ~55 chars < 50000 → 不触发再落盘
    text, extras = budget_tool_result_for_event(
        tool_name="t",
        tool_output=already,
        tool_call_id="c",
        enabled=True,
    )
    assert list(tmp_path.iterdir()) == []
    assert extras == {}
    # 干净渲染（preview + 已有 persisted 引用），非丑陋 dict repr
    assert "preview-text" in text
    assert "/existing/path" in text
    assert '"ok"' not in text  # 不是 json.dumps(dict)


def test_enabled_oversized_dict_serializes_json_preview(tmp_path) -> None:
    """原生 langgraph tool 返回的大 dict（未预算）→ json 序列化 preview + 落盘。"""
    big_dict = {"rows": ["item" * 10 for _ in range(50)]}
    text, extras = budget_tool_result_for_event(
        tool_name="t",
        tool_output=big_dict,
        tool_call_id="call_99",
        enabled=True,
        budget=_budget(tmp_path),
    )
    audit = extras["tool_result_budget"]
    assert audit["truncated"] is True
    assert audit["persisted"]["mime_type"] == "application/json"
    persisted = open(audit["persisted"]["path"], encoding="utf-8").read()
    assert "rows" in persisted  # 全文落盘


# --- streaming sink 集成 ---


@pytest.mark.asyncio
async def test_streaming_sink_bounds_text_for_ksadk_hosted(monkeypatch, tmp_path) -> None:
    """ksadk_hosted prepared + 巨大 tool_output → SessionEvent text 被 bound，metadata.tool_output 保留原值。"""
    from ksadk.sessions.in_memory import InMemorySessionService

    monkeypatch.setenv("KSADK_TOOL_RESULT_DIR", str(tmp_path))
    big = "y" * 60000  # 超过默认 50k 阈值 → 触发落盘 + 截断

    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="sess-c")

    # 直接调 helper 模拟 sink 行为（sink 逻辑 = 调 helper + append）。
    text, extras = budget_tool_result_for_event(
        tool_name="run_command",
        tool_output=big,
        tool_call_id="call_1",
        enabled=True,  # ksadk_hosted
    )
    from ksadk.conversations.runtime_persistence import append_conversation_event

    event = await append_conversation_event(
        session_id="sess-c",
        author="tool",
        role="user",
        text=text,
        invocation_id="inv-1",
        event_type="tool_result",
        metadata={"tool_name": "run_command", "tool_output": big, **extras},
        session_service_provider=lambda: service,
    )
    # content.parts[0].text 被 bound（远小于 60000）
    stored_text = event.content["parts"][0]["text"]
    assert len(stored_text) < 60000
    assert "[persisted-output]" in stored_text
    # metadata.tool_output 保留原值（UI/Responses 读取方不受影响）
    assert event.metadata["tool_output"] == big
    # 审计字段存在
    assert event.metadata["tool_result_budget"]["truncated"] is True


@pytest.mark.asyncio
async def test_streaming_sink_no_budget_for_framework_mode(monkeypatch, tmp_path) -> None:
    """非 ksadk_hosted prepared（framework）→ enabled=False → text 未 bound，字节级旧逻辑。"""
    from ksadk.conversations.runtime_persistence import append_conversation_event
    from ksadk.sessions.in_memory import InMemorySessionService

    monkeypatch.setenv("KSADK_TOOL_RESULT_DIR", str(tmp_path))
    big = "y" * 60000

    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="sess-f")

    # framework → enabled=False
    text, extras = budget_tool_result_for_event(
        tool_name="run_command",
        tool_output=big,
        tool_call_id="call_1",
        enabled=False,
    )
    event = await append_conversation_event(
        session_id="sess-f",
        author="tool",
        role="user",
        text=text,
        invocation_id="inv-1",
        event_type="tool_result",
        metadata={"tool_name": "run_command", "tool_output": big, **extras},
        session_service_provider=lambda: service,
    )
    # text == str(big)（未 bound，与今天一致）
    assert event.content["parts"][0]["text"] == big
    assert "tool_result_budget" not in event.metadata
    # 无落盘文件
    assert list(tmp_path.iterdir()) == []


def test_history_projection_uses_bounded_text(monkeypatch, tmp_path) -> None:
    """bound 后的 SessionEvent 经 project_model_messages → history 里 tool_result 体积显著下降。"""
    from ksadk.conversations.context import build_history_from_events
    from ksadk.sessions import SessionEvent

    monkeypatch.setenv("KSADK_TOOL_RESULT_DIR", str(tmp_path))
    big = "z" * 60000  # 超过默认 50k 阈值 → bound
    text, extras = budget_tool_result_for_event(
        tool_name="t", tool_output=big, tool_call_id="c1", enabled=True
    )
    event = SessionEvent.from_dict(
        {
            "id": "e1",
            "author": "tool",
            "event_type": "tool_result",
            "invocationId": "inv-1",
            "content": {"role": "user", "parts": [{"text": text}]},
            "timestamp": 1,
            "seqId": 1,  # seq_id>0，否则被 compaction 过滤跳过
            "stateDelta": {},
            "metadata": {"tool_output": big, **extras},
        },
        session_id="s",
    )
    history = build_history_from_events([event])
    assert len(history) == 1
    assert history[0]["role"] == "user"
    # history content 被 bound（远小于 60000），含 persisted 引用
    assert len(history[0]["content"]) < 60000
    assert "[persisted-output]" in history[0]["content"]


# --- 端到端 streaming sink 接线 ---


class _BigToolRunner:
    """伪 runner：stream 一个巨大 tool_result chunk + final。detection langgraph → capability ksadk-owned。"""

    def __init__(self, big: str) -> None:
        self._big = big
        self.detection_result = SimpleNamespace(
            type=SimpleNamespace(value="langgraph"), name="big-tool"
        )

    def load_agent(self) -> None:
        return None

    def prepare_for_request(self, _model: Any) -> None:
        return None

    async def stream(self, _payload: dict[str, Any]):
        yield {"type": "tool_call", "tool_name": "read_huge", "tool_args": {}, "run_id": "r1"}
        yield {
            "type": "tool_result",
            "tool_name": "read_huge",
            "tool_output": self._big,
            "run_id": "r1",
        }
        yield {"type": "final", "output": "done"}


async def _drain(stream) -> list:
    out = []
    async for ev in stream:
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_e2e_streaming_bounds_tool_result_for_ksadk_hosted(monkeypatch, tmp_path) -> None:
    """端到端：ksadk_hosted + 巨大 tool_output chunk → 落库 SessionEvent text 被 bound，metadata 保留原值。"""
    from ksadk.conversations.runtime_stream_events import _iter_conversation_turn_events
    from ksadk.sessions.in_memory import InMemorySessionService

    monkeypatch.setenv("KSADK_TOOL_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "1")
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    big = "y" * 60000

    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="sess-e2e-c")
    runner = _BigToolRunner(big)

    await _drain(
        _iter_conversation_turn_events(
            runner=runner,
            agent_id="a",
            user_id="u",
            session_id="sess-e2e-c",
            messages=[{"role": "user", "content": "读那个大文件"}],
            model="m",
            prepare_runner=lambda _r, _m: None,
            instructions="本次",
            agent_system="你是助手",
            agent_task="用中文",
            prompt_integration_mode="ksadk_hosted",  # ← 触发 PR C
            session_service_provider=lambda: service,
        )
    )
    events = await service.get_events("sess-e2e-c")
    tool_events = [e for e in events if e.event_type == "tool_result"]
    assert tool_events, "expected a persisted tool_result SessionEvent"
    ev = tool_events[0]
    stored_text = ev.content["parts"][0]["text"]
    # text 被 bound（远小于 60000），含 persisted 引用
    assert len(stored_text) < 60000
    assert "[persisted-output]" in stored_text
    # metadata.tool_output 保留原值（UI/Responses 读取方不受影响）
    assert ev.metadata["tool_output"] == big
    assert ev.metadata["tool_result_budget"]["truncated"] is True


@pytest.mark.asyncio
async def test_e2e_streaming_no_budget_for_framework(monkeypatch, tmp_path) -> None:
    """端到端：framework（prompt_integration_mode 空）→ text 未 bound，字节级旧逻辑。"""
    from ksadk.conversations.runtime_stream_events import _iter_conversation_turn_events
    from ksadk.sessions.in_memory import InMemorySessionService

    monkeypatch.setenv("KSADK_TOOL_RESULT_DIR", str(tmp_path))
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    big = "y" * 60000

    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="sess-e2e-f")
    runner = _BigToolRunner(big)

    await _drain(
        _iter_conversation_turn_events(
            runner=runner,
            agent_id="a",
            user_id="u",
            session_id="sess-e2e-f",
            messages=[{"role": "user", "content": "读那个大文件"}],
            model="m",
            prepare_runner=lambda _r, _m: None,
            instructions="本次",
            agent_system="你是助手",
            prompt_integration_mode="",  # framework → 不接管
            session_service_provider=lambda: service,
        )
    )
    events = await service.get_events("sess-e2e-f")
    tool_events = [e for e in events if e.event_type == "tool_result"]
    assert tool_events
    ev = tool_events[0]
    # text == str(big)（未 bound，与今天一致）
    assert ev.content["parts"][0]["text"] == big
    assert "tool_result_budget" not in ev.metadata
    assert list(tmp_path.iterdir()) == []  # 无落盘
