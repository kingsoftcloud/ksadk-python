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
        input_parts=[],
        attachments=[],
        attachment_results=[],
        runner_type="langgraph",
        model="gpt-4o",
        model_options={"thinking": {"type": "disabled"}},
    )


def test_chat_openai_patch_maps_request_model_options_for_chat_completions():
    apply_patch()
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-test", use_responses_api=False)

    with platform_invocation_scope(_context()):
        payload = llm._get_request_payload([HumanMessage(content="hello")])

    assert payload["reasoning_effort"] == "none"
    assert payload["extra_body"]["thinking"] == {"type": "disabled"}
    assert payload["extra_body"]["max_reasoning_tokens"] == 0


def test_chat_openai_patch_maps_request_model_options_for_responses_api():
    apply_patch()
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-test", use_responses_api=True)

    with platform_invocation_scope(_context()):
        payload = llm._get_request_payload([HumanMessage(content="hello")])

    assert payload["reasoning"] == {"effort": "none"}
    assert payload["extra_body"]["thinking"] == {"type": "disabled"}
    assert payload["extra_body"]["max_reasoning_tokens"] == 0
