"""
Monkey patch for langchain-openai to support reasoning_content field (e.g. for DeepSeek R1, GLM-4.7)
"""

import logging
from typing import Any, Mapping, cast

from ksadk.conversations.model_options import (
    model_options_for_chat_completions,
    model_options_for_responses,
)
from ksadk.runtime_context import get_current_invocation_context

from langchain_core.messages import (
    AIMessageChunk,
    BaseMessageChunk,
    ChatMessageChunk,
    FunctionMessageChunk,
    HumanMessageChunk,
    SystemMessageChunk,
    ToolMessageChunk,
)
from langchain_core.messages.tool import tool_call_chunk

logger = logging.getLogger(__name__)

# Original function reference (to avoid recursion if patched multiple times)
_original_convert_delta = None


def _patched_convert_delta_to_message_chunk(
    _dict: Mapping[str, Any], default_class: type[BaseMessageChunk]
) -> BaseMessageChunk:
    """Patched version to include reasoning_content in additional_kwargs."""
    id_ = _dict.get("id")
    role = cast(str, _dict.get("role"))
    content = cast(str, _dict.get("content") or "")
    additional_kwargs: dict = {}

    # === PATCH START: Capture reasoning_content ===
    if reasoning_content := _dict.get("reasoning_content"):
        additional_kwargs["reasoning_content"] = reasoning_content
    # === PATCH END ===

    if _dict.get("function_call"):
        function_call = dict(_dict["function_call"])
        if "name" in function_call and function_call["name"] is None:
            function_call["name"] = ""
        additional_kwargs["function_call"] = function_call

    tool_call_chunks = []
    if raw_tool_calls := _dict.get("tool_calls"):
        try:
            tool_call_chunks = [
                tool_call_chunk(
                    name=rtc["function"].get("name"),
                    args=rtc["function"].get("arguments"),
                    id=rtc.get("id"),
                    index=rtc["index"],
                )
                for rtc in raw_tool_calls
            ]
        except KeyError:
            pass

    if role == "user" or default_class == HumanMessageChunk:
        return HumanMessageChunk(content=content, id=id_)
    if role == "assistant" or default_class == AIMessageChunk:
        return AIMessageChunk(
            content=content,
            additional_kwargs=additional_kwargs,
            id=id_,
            tool_call_chunks=tool_call_chunks,  # type: ignore[arg-type]
        )
    if role in ("system", "developer") or default_class == SystemMessageChunk:
        if role == "developer":
            additional_kwargs = {"__openai_role__": "developer"}
        else:
            additional_kwargs = {}
        return SystemMessageChunk(content=content, id=id_, additional_kwargs=additional_kwargs)
    if role == "function" or default_class == FunctionMessageChunk:
        return FunctionMessageChunk(content=content, name=_dict["name"], id=id_)
    if role == "tool" or default_class == ToolMessageChunk:
        return ToolMessageChunk(content=content, tool_call_id=_dict["tool_call_id"], id=id_)
    if role or default_class == ChatMessageChunk:
        return ChatMessageChunk(content=content, role=role, id=id_)
    return default_class(content=content, id=id_)  # type: ignore[call-arg]


_original_convert_message_to_dict = None
_original_base_get_request_payload = None
_original_chat_get_request_payload = None


def _patched_convert_message_to_dict(message, *args, **kwargs):
    """Patched version to include reasoning_content in outgoing requests."""
    from langchain_core.messages import AIMessage
    
    # Call original function first
    result = _original_convert_message_to_dict(message, *args, **kwargs)
    
    # If this is an AIMessage with tool_calls and reasoning_content in additional_kwargs
    if isinstance(message, AIMessage):
        additional_kwargs = getattr(message, 'additional_kwargs', {}) or {}
        reasoning = additional_kwargs.get('reasoning_content')
        
        # Only add if we have tool_calls (required by some thinking models)
        if message.tool_calls and reasoning is not None:
            result['reasoning_content'] = reasoning
        elif message.tool_calls and reasoning is None:
            # Force add empty reasoning_content for thinking models
            result['reasoning_content'] = ''
    
    return result


def _merge_payload_options(payload: dict[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    for key, value in options.items():
        if key == "extra_body" and isinstance(value, Mapping):
            extra_body = dict(merged.get("extra_body") or {})
            for extra_key, extra_value in value.items():
                extra_body.setdefault(extra_key, extra_value)
            merged["extra_body"] = extra_body
            continue
        merged.setdefault(key, value)
    return merged


def _current_request_model_options() -> dict[str, Any]:
    context = get_current_invocation_context()
    if context is None:
        return {}
    return dict(context.model_options or {})


def _patched_base_get_request_payload(self, input_, *, stop=None, **kwargs):
    payload = _original_base_get_request_payload(self, input_, stop=stop, **kwargs)
    model_options = _current_request_model_options()
    if not model_options:
        return payload
    if self._use_responses_api(payload):
        return _merge_payload_options(payload, model_options_for_responses(model_options))
    return _merge_payload_options(payload, model_options_for_chat_completions(model_options))


def _patched_chat_get_request_payload(self, input_, *, stop=None, **kwargs):
    payload = _original_chat_get_request_payload(self, input_, stop=stop, **kwargs)
    model_options = _current_request_model_options()
    if not model_options:
        return payload
    if self._use_responses_api(payload):
        return _merge_payload_options(payload, model_options_for_responses(model_options))
    return _merge_payload_options(payload, model_options_for_chat_completions(model_options))


def apply_patch():
    """Apply the monkey patch to langchain_openai."""
    global _original_convert_delta, _original_convert_message_to_dict
    global _original_base_get_request_payload, _original_chat_get_request_payload
    try:
        import langchain_openai.chat_models.base as base_module

        # Only patch if not already patched
        if getattr(base_module, "_ksadk_patched", False):
            return

        # Patch 1: Fix receiving responses (capture reasoning_content)
        _original_convert_delta = base_module._convert_delta_to_message_chunk
        base_module._convert_delta_to_message_chunk = _patched_convert_delta_to_message_chunk
        
        # Patch 2: Fix sending requests (include reasoning_content for tool_call messages)
        _original_convert_message_to_dict = base_module._convert_message_to_dict
        base_module._convert_message_to_dict = _patched_convert_message_to_dict

        _original_base_get_request_payload = base_module.BaseChatOpenAI._get_request_payload
        base_module.BaseChatOpenAI._get_request_payload = _patched_base_get_request_payload
        _original_chat_get_request_payload = base_module.ChatOpenAI._get_request_payload
        base_module.ChatOpenAI._get_request_payload = _patched_chat_get_request_payload
        
        base_module._ksadk_patched = True

        # logger.info("Applied langchain-openai patch for reasoning_content support")
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Failed to patch langchain-openai: {e}")
