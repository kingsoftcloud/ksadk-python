from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from ksadk.runtime_context import PlatformInvocationContext, platform_invocation_scope
from ksadk.runners.patch_langchain import apply_patch


def _context() -> PlatformInvocationContext:
    return PlatformInvocationContext(
        agent_id="demo-agent",
        user_id="user",
        session_id="sess-1",
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
        model="gpt-4o",
        model_options={"thinking": {"type": "disabled"}},
    )


def test_chat_openai_patch_maps_request_model_options_for_chat_completions():
    apply_patch()
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-test", use_responses_api=False)

    with platform_invocation_scope(_context()):
        payload = llm._get_request_payload([HumanMessage(content="hello")])

    assert "reasoning_effort" not in payload
    assert payload["extra_body"]["max_reasoning_tokens"] == 0
    assert "thinking" not in payload["extra_body"]


def test_chat_openai_patch_keeps_supported_reasoning_effort_for_chat_completions():
    apply_patch()
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-test", use_responses_api=False)
    context = _context()
    context.model_options = {"reasoning": {"effort": "low"}}

    with platform_invocation_scope(context):
        payload = llm._get_request_payload([HumanMessage(content="hello")])

    assert payload["reasoning_effort"] == "low"


def test_chat_openai_patch_maps_enabled_thinking_to_reasoning_effort_for_chat_completions():
    apply_patch()
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-test", use_responses_api=False)
    context = _context()
    context.model_options = {"thinking": {"type": "enabled"}}

    with platform_invocation_scope(context):
        payload = llm._get_request_payload([HumanMessage(content="hello")])

    assert payload["reasoning_effort"] == "medium"
    assert "extra_body" not in payload or "thinking" not in payload.get("extra_body", {})


def test_chat_openai_patch_maps_request_model_options_for_responses_api():
    apply_patch()
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-test", use_responses_api=True)

    with platform_invocation_scope(_context()):
        payload = llm._get_request_payload([HumanMessage(content="hello")])

    assert payload["reasoning"] == {"effort": "none"}
    assert payload["extra_body"]["thinking"] == {"type": "disabled"}
    assert payload["extra_body"]["max_reasoning_tokens"] == 0


def test_chat_openai_patch_preserves_temperature_override():
    apply_patch()
    llm = ChatOpenAI(model="kimi-k2.7-code", api_key="sk-test", use_responses_api=False)
    context = _context()
    context.model = "kimi-k2.7-code"
    context.model_options = {"temperature": 1}

    with platform_invocation_scope(context):
        payload = llm._get_request_payload([HumanMessage(content="hello")])

    assert payload["temperature"] == 1
