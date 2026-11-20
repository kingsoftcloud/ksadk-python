"""Compatibility with LiteLLM function calls whose type is empty."""

from functools import wraps


def normalize_tool_call_types(message):
    calls = (
        message.get("tool_calls")
        if isinstance(message, dict)
        else getattr(message, "tool_calls", None)
    )
    for call in calls or ():
        if isinstance(call, dict):
            if call.get("type") in (None, "") and call.get("function") is not None:
                call["type"] = "function"
        elif (
            getattr(call, "type", None) in (None, "")
            and getattr(call, "function", None) is not None
        ):
            call.type = "function"


def normalize_tool_delta_types(module):
    convert = getattr(module, "_model_response_to_chunk", None)
    if convert is None or getattr(convert, "_ksadk_tool_delta_types", False):
        return

    @wraps(convert)
    def convert_stream_chunk(response, *args, **kwargs):
        for choice in response.get("choices") or ():
            normalize_tool_call_types(choice.get("delta") or choice.get("message"))
        yield from convert(response, *args, **kwargs)

    convert_stream_chunk._ksadk_tool_delta_types = True
    module._model_response_to_chunk = convert_stream_chunk
