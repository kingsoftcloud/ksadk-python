"""Validate supplied provider identity before projecting a model response."""

from dataclasses import dataclass

from ksadk._model_json import load_model_json
from ksadk.studio.errors import StudioError


def invalid_model_frame() -> StudioError:
    return StudioError("MODEL_RESPONSE_INVALID", "Invalid model response frame", status_code=422)


def decode_model_frame(raw: str | bytes) -> dict:
    try:
        value = load_model_json(raw)
    except (ValueError, TypeError, RecursionError):
        raise invalid_model_frame() from None
    if not isinstance(value, dict):
        raise invalid_model_frame()
    return value


def _object(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise invalid_model_frame()
    return value


def _text(value: object) -> None:
    if value is not None and type(value) is not str:
        raise invalid_model_frame()


def _message(value: object) -> None:
    message = _object(value)
    role = message.get("role")
    if role is not None and role != "assistant":
        raise invalid_model_frame()
    for name in ("content", "reasoning", "reasoning_content", "refusal"):
        _text(message.get(name))


def chat_choices(payload: dict) -> list[dict]:
    """Select canonical choice zero; alternate choices never enter its batch.

    A proxy which omits indexes retains the existing first-choice convention.
    Explicit indexes are genuine nonnegative integers and take precedence over
    array order. Validate declared fields even on an unselected alternative.
    """
    choices = payload.get("choices")
    if choices is None:
        return []
    if not isinstance(choices, list):
        raise invalid_model_frame()
    zero = None
    unindexed = None
    seen = set()
    for choice in choices:
        if not isinstance(choice, dict):
            raise invalid_model_frame()
        index = choice.get("index")
        if index is not None:
            if type(index) is not int or index < 0 or index in seen:
                raise invalid_model_frame()
            seen.add(index)
            if index == 0:
                zero = choice
        elif unindexed is None:
            unindexed = choice
        _text(choice.get("finish_reason"))
        _message(choice.get("delta"))
        _message(choice.get("message"))
    selected = zero if zero is not None else unindexed
    return [] if selected is None else [selected]


@dataclass
class ModelWireIdentity:
    """One physical HTTP attempt; no identity is inferred when it is absent.

    Provider aliases need not equal the host's requested model name, but a
    supplied model and response identity cannot change within the attempt.
    This does not authenticate an upstream model or replace Build/Scope pins.
    """

    response_id: str | None = None
    model: str | None = None

    def _pin(self, field: str, value: object) -> None:
        if value is None:
            return
        if type(value) is not str or not value.strip():
            raise invalid_model_frame()
        previous = getattr(self, field)
        if previous is not None and previous != value:
            raise invalid_model_frame()
        setattr(self, field, value)

    def accept(self, payload: dict, *, wire_api: str) -> None:
        if not isinstance(payload, dict):
            raise invalid_model_frame()
        if wire_api == "chat":
            self._pin("response_id", payload.get("id"))
            self._pin("model", payload.get("model"))
            _message(payload.get("message"))
            return
        kind = payload.get("type")
        if type(kind) is not str or not kind.strip():
            raise invalid_model_frame()
        if wire_api == "responses":
            response = _object(payload.get("response"))
            self._pin("response_id", response.get("id"))
            self._pin("response_id", payload.get("response_id"))
            self._pin("model", response.get("model"))
            if kind in {
                "response.output_text.delta", "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta", "response.function_call_arguments.delta",
                "response.refusal.delta",
            }:
                _text(payload.get("delta"))
            return
        if kind == "message_start":
            message = _object(payload.get("message"))
            role = message.get("role")
            if role is not None and role != "assistant":
                raise invalid_model_frame()
            self._pin("response_id", message.get("id"))
            self._pin("model", message.get("model"))
        if kind == "content_block_delta":
            delta = _object(payload.get("delta"))
            for field in ("text", "thinking", "partial_json"):
                _text(delta.get(field))
        if kind == "message_delta":
            delta = _object(payload.get("delta"))
            for field in ("stop_reason", "stop_sequence"):
                _text(delta.get(field))
