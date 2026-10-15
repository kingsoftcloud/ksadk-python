"""Typed projections of complete HTTP bodies, before model/tool admission.

This is a provider decoder, not a second runtime state store. Declared values
are validated before convenience fields or empty defaults can conceal them.
"""

import json
from dataclasses import dataclass, field

from ksadk._model_refusal import anthropic_refusal, chat_refusal
from ksadk.studio.model_content_stream import _reasoning
from ksadk.studio.model_wire_identity import ModelWireIdentity, chat_choices, invalid_model_frame
from ksadk.studio.model_wire_tools import validate_client_item_type


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise invalid_model_frame()
    return value


def _array(value: object) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise invalid_model_frame()
    return value


def _text(value: object, *, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if type(value) is not str:
        raise invalid_model_frame()
    return value


def _identity(value: object) -> str:
    result = _text(value)
    if not result.strip():
        raise invalid_model_frame()
    return result


def _role(value: object) -> None:
    if value is not None and value != "assistant":
        raise invalid_model_frame()


def _parts(value: object, types: set[str]) -> str:
    result = []
    for raw in _array(value):
        part = _object(raw)
        kind = _identity(part.get("type"))
        if kind in types:
            result.append(_text(part.get("text")))
    return "".join(result)


def response_message_content(value: object) -> tuple[str, bool]:
    texts = []
    refused = False
    for raw in _array(value):
        part = _object(raw)
        kind = _identity(part.get("type"))
        if kind in {"output_text", "text"}:
            texts.append(_text(part.get("text")))
        elif kind == "refusal":
            _text(part.get("refusal"))
            refused = True
    return "".join(texts), refused


def wire_refusal(value: object, *, wire_api: str) -> bool:
    try:
        return anthropic_refusal(value) if wire_api == "anthropic" else chat_refusal(value)
    except ValueError:
        raise invalid_model_frame() from None


@dataclass(frozen=True)
class BufferedCall:
    call_id: str
    name: str
    arguments: str
    raw: dict


@dataclass
class BufferedProjection:
    content: str = ""
    reasoning: str = ""
    finish: str = ""
    calls: list[BufferedCall] = field(default_factory=list)
    raw_message: dict = field(default_factory=dict)


def validate_buffered_identity(payload: dict, *, wire_api: str) -> None:
    if wire_api not in {"chat", "responses", "anthropic"}:
        raise ValueError("unsupported buffered model wire")
    identity = ModelWireIdentity()
    if wire_api == "chat":
        identity.accept(payload, wire_api="chat")
    elif wire_api == "responses":
        identity.accept({"type": "response.created", "response": payload}, wire_api="responses")
    else:
        identity.accept({"type": "message_start", "message": payload}, wire_api="anthropic")


def buffered_projection(payload: dict, *, wire_api: str) -> BufferedProjection:
    validate_buffered_identity(payload, wire_api=wire_api)
    if payload.get("error") is not None and (
        wire_api != "responses" or payload.get("status") == "completed"
    ):
        raise invalid_model_frame()
    result = BufferedProjection()
    if wire_api == "chat":
        choices = chat_choices(payload)
        if not choices:
            raise invalid_model_frame()
        choice = choices[0]
        message = _object(choice.get("message"))
        if message.get("function_call") is not None:
            raise invalid_model_frame()
        for raw in _array(message.get("tool_calls")):
            call = _object(raw)
            if call.get("type") not in (None, "function"):
                raise invalid_model_frame()
            function = _object(call.get("function"))
            result.calls.append(BufferedCall(
                _identity(call.get("id")), _identity(function.get("name")),
                _text(function.get("arguments")), call,
            ))
        result.content = _text(message.get("content"), optional=True)
        result.reasoning = _reasoning(message)
        result.finish = _identity(choice.get("finish_reason"))
        if wire_refusal(message.get("refusal"), wire_api="chat"):
            result.finish, result.calls = "content_filter", []
        result.raw_message = message
        return result
    if wire_api == "responses":
        status = _identity(payload.get("status"))
        if "output" in payload and not isinstance(payload["output"], list):
            raise invalid_model_frame()
        output = _array(payload.get("output"))
        messages, reasoning = [], []
        refused = False
        for raw in output:
            item = _object(raw)
            kind = _identity(item.get("type"))
            validate_client_item_type(kind, wire_api="responses")
            if "status" in item:
                _text(item["status"], optional=True)
            if (status == "completed" and kind in {"message", "function_call"}
                    and item.get("status") not in (None, "completed")):
                raise invalid_model_frame()
            if kind == "function_call":
                result.calls.append(BufferedCall(
                    _identity(item.get("call_id")), _identity(item.get("name")),
                    _text(item.get("arguments")), item,
                ))
            elif kind == "message":
                _role(item.get("role"))
                text, item_refused = response_message_content(item.get("content"))
                messages.append(text)
                refused = refused or item_refused
            elif kind == "reasoning":
                reasoning.append(_parts(item.get("summary"), {"summary_text", "text"}))
                reasoning.append(_parts(item.get("content"), {"reasoning_text", "text"}))
        canonical = "".join(messages)
        convenience = _text(payload.get("output_text"), optional=True)
        if "output" in payload and payload.get("output_text") is not None \
                and canonical != convenience:
            raise invalid_model_frame()
        result.content = canonical or convenience
        result.reasoning = "".join(reasoning)
        result.finish = (
            ("tool_calls" if result.calls else "stop") if status == "completed" else status
        )
        if status == "incomplete":
            details = payload.get("incomplete_details")
            reason = (
                _text(_object(details).get("reason"), optional=True) if details is not None else ""
            )
            result.finish = (
                "content_filter" if reason == "content_filter"
                else "length" if reason == "max_output_tokens" and not result.calls
                else "incomplete"
            )
        if refused:
            result.finish, result.calls = "content_filter", []
        result.raw_message = {"output": output}
        return result
    if "type" in payload and payload["type"] != "message":
        raise invalid_model_frame()
    _text(payload.get("stop_sequence"), optional=True)
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise invalid_model_frame()
    texts, thoughts = [], []
    for raw in blocks:
        block = _object(raw)
        kind = _identity(block.get("type"))
        validate_client_item_type(kind, wire_api="anthropic")
        if kind == "text":
            texts.append(_text(block.get("text")))
        elif kind == "thinking":
            thoughts.append(_text(block.get("thinking")))
            if "signature" in block:
                _text(block["signature"])
        elif kind == "redacted_thinking":
            _text(block.get("data"))
        elif kind == "tool_use":
            result.calls.append(BufferedCall(
                _identity(block.get("id")), _identity(block.get("name")),
                json.dumps(_object(block.get("input")), ensure_ascii=False), block,
            ))
    stop = _identity(payload.get("stop_reason"))
    result.finish = {
        "end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
        "tool_use": "tool_calls", "refusal": "content_filter",
    }.get(stop, stop)
    if wire_refusal(payload.get("stop_details"), wire_api="anthropic"):
        result.finish, result.calls = "content_filter", []
    result.content, result.reasoning = "".join(texts), "".join(thoughts)
    result.raw_message = {"content": blocks}
    return result
