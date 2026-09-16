from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from ksadk.runners.patch_langchain import apply_patch
from ksadk.runtime_context import PlatformInvocationContext, platform_invocation_scope


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
    assert payload["extra_body"]["enable_thinking"] is False
    assert payload["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
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
    assert payload["extra_body"]["enable_thinking"] is False
    assert payload["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_chat_openai_patch_preserves_temperature_override():
    apply_patch()
    llm = ChatOpenAI(model="kimi-k2.7-code", api_key="sk-test", use_responses_api=False)
    context = _context()
    context.model = "kimi-k2.7-code"
    context.model_options = {"temperature": 1}

    with platform_invocation_scope(context):
        payload = llm._get_request_payload([HumanMessage(content="hello")])

    assert payload["temperature"] == 1


def test_patch_deduplicates_consecutive_identical_usage():
    """When upstream sends identical usage in both the finish chunk and the
    trailing choices=[] chunk, the second occurrence must be suppressed so
    LangChain-core does not sum them into 2x."""
    apply_patch()
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-test", use_responses_api=False)

    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    finish_chunk = {
        "id": "chatcmpl-test-dedup-1",
        "choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": dict(usage),
    }
    gen = llm._convert_chunk_to_generation_chunk(finish_chunk, AIMessageChunk, {})
    assert gen is not None
    assert gen.message.usage_metadata is not None
    assert gen.message.usage_metadata["input_tokens"] == 10
    assert gen.message.usage_metadata["output_tokens"] == 5

    final_chunk = {
        "id": "chatcmpl-test-dedup-1",
        "choices": [],
        "usage": dict(usage),
    }
    gen = llm._convert_chunk_to_generation_chunk(final_chunk, AIMessageChunk, {})
    assert gen is not None
    assert gen.message.usage_metadata is None


def test_patch_preserves_distinct_usage():
    """When two chunks carry different usage values, both must be preserved."""
    apply_patch()
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-test", use_responses_api=False)

    usage_1 = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    usage_2 = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}

    finish_chunk = {
        "id": "chatcmpl-test-distinct-1",
        "choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": dict(usage_1),
    }
    gen = llm._convert_chunk_to_generation_chunk(finish_chunk, AIMessageChunk, {})
    assert gen is not None
    assert gen.message.usage_metadata is not None
    assert gen.message.usage_metadata["input_tokens"] == 10

    final_chunk = {
        "id": "chatcmpl-test-distinct-1",
        "choices": [],
        "usage": dict(usage_2),
    }
    gen = llm._convert_chunk_to_generation_chunk(final_chunk, AIMessageChunk, {})
    assert gen is not None
    assert gen.message.usage_metadata is not None
    assert gen.message.usage_metadata["input_tokens"] == 20
