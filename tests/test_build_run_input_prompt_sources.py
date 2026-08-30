"""PR A：build_run_input 接入 agent_system/agent_task + 不改 Runner 输入 单测。"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from ksadk.conversations.runtime_input import _build_runner_request_payload
from ksadk.conversations.runtime_payloads import PreparedConversationTurn
from ksadk.conversations.runtime_preparation import build_run_input
from ksadk.runtime_context import PlatformInvocationContext
from ksadk.sessions.in_memory import InMemorySessionService


async def _build(
    *,
    agent_system: str = "",
    agent_task: str = "",
    instructions: str = "",
    session_id: str = "sess-prompt-sources",
) -> PreparedConversationTurn:
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id=session_id)
    return await build_run_input(
        agent_id="a",
        user_id="u",
        session_id=session_id,
        messages=[{"role": "user", "content": "帮我做个总结"}],
        instructions=instructions,
        agent_system=agent_system,
        agent_task=agent_task,
        session_service_provider=lambda: service,
        runtime_type="langgraph",
    )


@pytest.mark.asyncio
async def test_agent_sources_produce_compiled_prompt_with_stable_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    prepared = await _build(
        agent_system="你是助手",
        agent_task="用中文",
        instructions="本次问题",
    )
    assert prepared.compiled_prompt is not None
    assert prepared.compiled_prompt["prompt_stable_prefix_hash"].startswith("sha256:")
    # shadow_context_plan 的 stable_prefix_hash 与真实 compiled_prompt 一致（prompt_shadow override 生效）。
    assert (
        prepared.shadow_context_plan["prompt_stable_prefix_hash"]
        == prepared.compiled_prompt["prompt_stable_prefix_hash"]
    )
    assert set(prepared.compiled_prompt["prompt_section_hashes"]) == {
        "agent_identity",
        "agent_policy",
        "request_instructions",
    }


@pytest.mark.asyncio
async def test_no_agent_sources_falls_back_to_instructions_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_system/agent_task 全空 → compiled_prompt None，shadow 行为与 PR2 一致（回退保护）。"""
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    prepared = await _build(instructions="本次问题")
    assert prepared.compiled_prompt is None
    # shadow plan 仍 instructions-only：stable_prefix_hash 为空（仅 volatile）。
    assert prepared.shadow_context_plan["prompt_stable_prefix_hash"] == ""
    assert "request_instructions" in prepared.shadow_context_plan["prompt_section_hashes"]


@pytest.mark.asyncio
async def test_runner_payload_unchanged_by_compiled_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR A 核心不变量：payload["instructions"] 仍由 request instructions 决定，不含 compiled_prompt。"""
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    prepared = await _build(
        agent_system="你是助手",
        agent_task="用中文",
        instructions="本次问题",
    )
    ctx = PlatformInvocationContext(
        agent_id="a",
        user_id="u",
        account_id="",
        session_id=prepared.session_id,
        history=list(prepared.history),
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
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=ctx, runner=None
    )
    assert payload["instructions"] == "本次问题"
    assert "compiled_prompt" not in payload
    assert "agent_system" not in payload
    assert "agent_task" not in payload


@pytest.mark.asyncio
async def test_platform_safety_section_only_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_PLATFORM_SAFETY_TEXT", "平台安全规则")
    prepared = await _build(agent_system="你是助手")
    assert prepared.compiled_prompt is not None
    assert "platform_safety" in prepared.compiled_prompt["prompt_section_hashes"]

    # env 未设 → 不含 platform_safety
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    prepared2 = await _build(agent_system="你是助手", session_id="sess-prompt-sources-2")
    assert prepared2.compiled_prompt is not None
    assert "platform_safety" not in prepared2.compiled_prompt["prompt_section_hashes"]


@pytest.mark.asyncio
async def test_compiled_prompt_roundtrips_through_asdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """canonical 路径 asdict(prepared) → prepared_turn → PreparedConversationTurn(**dict) 保留 compiled_prompt。"""
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    prepared = await _build(agent_system="你是助手", agent_task="用中文")
    raw = asdict(prepared)
    restored = PreparedConversationTurn(**dict(raw))
    assert restored.compiled_prompt == prepared.compiled_prompt


@pytest.mark.asyncio
async def test_resume_paths_have_no_compiled_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """checkpoint/approval resume 旁路不重编真实 CompiledPrompt（compiled_prompt None）。"""
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="sess-resume-cp")
    prepared = await build_run_input(
        agent_id="a",
        user_id="u",
        session_id="sess-resume-cp",
        messages=[],
        agent_system="你是助手",
        agent_task="用中文",
        resume_input={
            "type": "agentengine.resume_checkpoint",
            "run_id": "r1",
            "checkpoint_id": "c1",
            "resume_attempt_id": "ra1",
            "framework": "langgraph",
            "framework_ref": {"langgraph": {"checkpoint_id": "c1", "thread_id": "sess-resume-cp"}},
        },
        invocation_id="inv-1",
        session_service_provider=lambda: service,
        runtime_type="langgraph",
    )
    assert prepared.compiled_prompt is None
    assert prepared.run_trigger == "checkpoint_resume"
