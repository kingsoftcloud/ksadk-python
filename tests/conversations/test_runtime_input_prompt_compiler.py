"""PR B：_should_project_compiled_prompt 三重门控 + payload 接管/回退字节级一致。"""

from __future__ import annotations

from types import SimpleNamespace

from ksadk.conversations.runtime_input import _build_runner_request_payload
from ksadk.conversations.runtime_payloads import PreparedConversationTurn
from ksadk.prompts.resolved import ResolvedPromptSources, compile_resolved_prompt_dict
from ksadk.runtime_context import PlatformInvocationContext


def _runner(langgraph: bool = True) -> SimpleNamespace:
    value = "langgraph" if langgraph else "adk"
    return SimpleNamespace(detection_result=SimpleNamespace(type=SimpleNamespace(value=value)))


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


def _prepared(
    monkeypatch, *, mode: str, instructions: str = "本轮指令", compiled: bool = True
) -> PreparedConversationTurn:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    compiled_dict = None
    if compiled:
        compiled_dict = compile_resolved_prompt_dict(
            ResolvedPromptSources(
                agent_system="你是助手", agent_task="用中文", request_instructions=instructions
            )
        )
    return PreparedConversationTurn(
        session_id="sess-projection",
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
        instructions=instructions,
        compiled_prompt=compiled_dict,
        prompt_integration_mode=mode,
    )


def test_flag_off_keeps_legacy_instructions(monkeypatch) -> None:
    monkeypatch.delenv("KSADK_PROMPT_COMPILER_ENABLED", raising=False)
    prepared = _prepared(monkeypatch, mode="ksadk_hosted")
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=_ctx(prepared.session_id), runner=_runner()
    )
    # flag 关 → 旧逻辑：payload instructions == request instructions，不含 compiled 正文
    assert payload["instructions"] == "本轮指令"


def test_flag_on_ksadk_hosted_langgraph_projects_compiled_content(monkeypatch) -> None:
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "1")
    prepared = _prepared(monkeypatch, mode="ksadk_hosted")
    payload = _build_runner_request_payload(
        prepared=prepared,
        model="m",
        runtime_context=_ctx(prepared.session_id),
        runner=_runner(True),
    )
    # 接管：instructions == CompiledPrompt.content（XML），agent_system/task 首次进输入
    assert payload["instructions"] == prepared.compiled_prompt["prompt_content"]
    assert "<agent_identity>" in payload["instructions"]
    assert "<agent_policy>" in payload["instructions"]


def test_flag_on_framework_mode_keeps_legacy(monkeypatch) -> None:
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "1")
    prepared = _prepared(monkeypatch, mode="")  # framework-owned
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=_ctx(prepared.session_id), runner=_runner()
    )
    assert payload["instructions"] == "本轮指令"


def test_flag_on_adk_runner_keeps_legacy(monkeypatch) -> None:
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "1")
    prepared = _prepared(monkeypatch, mode="ksadk_hosted")
    payload = _build_runner_request_payload(
        prepared=prepared,
        model="m",
        runtime_context=_ctx(prepared.session_id),
        runner=_runner(False),
    )
    # adk runner → 接管错位，排除 → 旧逻辑
    assert payload["instructions"] == "本轮指令"


def test_flag_on_compiled_none_resume_keeps_legacy(monkeypatch) -> None:
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "1")
    # resume 旁路：compiled_prompt=None + mode 空（双保险）
    prepared = _prepared(monkeypatch, mode="", compiled=False)
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=_ctx(prepared.session_id), runner=_runner()
    )
    assert payload["instructions"] == "本轮指令"
