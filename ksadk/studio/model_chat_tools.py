"""Request-local Chat tool inputs; snapshots complete facts, never replace them."""

import json
from dataclasses import dataclass, field

from ksadk._model_json import load_model_json
from ksadk.studio.errors import StudioError
from ksadk.studio.model_wire_identity import invalid_model_frame
from ksadk.studio.model_wire_tools import WireTool, _identity, _index, _object, _text


def _same_value(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    return left == right


def _alias(function: dict, call: dict, names: tuple[str, ...]) -> object:
    values = [source[name] for source in (function, call) for name in names
              if source.get(name) is not None]
    if not values:
        return None
    first = values[0]
    if any(not _same_value(value, first) for value in values[1:]):
        raise invalid_model_frame()
    return first


def _snapshot_arguments(value: object, observed: str) -> str:
    if isinstance(value, dict):
        # Existing proxy snapshots may carry an object. Preserve the observed
        # spelling when the complete JSON object agrees; whitespace is not a
        # change of executable input. Partial JSON still requires a matching
        # encoded prefix, never a speculative repair.
        if observed:
            try:
                parsed = load_model_json(observed)
            except (TypeError, ValueError):
                pass
            else:
                if _same_value(parsed, value):
                    return observed
        return json.dumps(value, ensure_ascii=False)
    return _text(value)


@dataclass
class ChatToolCalls:
    calls: dict[int, WireTool] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)
    finished: bool = False

    def accept(self, container: object, *, snapshot: bool) -> None:
        if container is None:
            return
        message = _object(container)
        # Legacy function_call carries no tool id. Never synthesize one or
        # let it overwrite an identified native call in the same response.
        if message.get("function_call") is not None:
            raise invalid_model_frame()
        values = message.get("tool_calls")
        if values is None:
            return
        if not isinstance(values, list):
            raise invalid_model_frame()
        seen = set()
        for value in values:
            raw = _object(value)
            function = _object(raw["function"]) if raw.get("function") is not None else {}
            index = _index(raw, "index", required=not snapshot)
            identity = _identity(raw["id"]) if raw.get("id") is not None else ""
            kind = raw.get("type")
            if kind is not None and (type(kind) is not str or kind != "function"):
                raise invalid_model_frame()
            name_value = _alias(function, raw, ("name",))
            name = _text(name_value) if name_value is not None else ""
            if name:
                _identity(name)
            elif snapshot and name_value is not None:
                raise invalid_model_frame()
            arguments = _alias(function, raw, ("arguments", "input"))
            if index is None:
                # Unindexed complete proxy snapshots retain stable id matching
                # and parallel calls, rather than repeatedly reusing slot 0.
                _identity(identity)
                index = self.ids.get(identity, max(self.calls, default=-1) + 1)
            if snapshot and index in seen:
                raise invalid_model_frame()
            seen.add(index)
            current = self.calls.get(index)
            if current is None:
                if self.finished and not snapshot:
                    raise invalid_model_frame()
                current = WireTool()
                self.calls[index] = current
            if identity:
                if self.ids.get(identity, index) != index:
                    raise invalid_model_frame()
                if current.call_id and current.call_id != identity:
                    raise invalid_model_frame()
                self.ids[identity] = index
            for field_name, incoming in (("call_id", identity), ("name", name)):
                previous = getattr(current, field_name)
                if incoming and previous and incoming != previous:
                    raise invalid_model_frame()
                if incoming and not previous:
                    if (current.closed or self.finished) and not snapshot:
                        raise invalid_model_frame()
                    setattr(current, field_name, incoming)
            if arguments is None:
                continue
            if snapshot:
                text = _snapshot_arguments(arguments, current.arguments)
                if current.closed:
                    if text != current.arguments:
                        raise invalid_model_frame()
                elif not text.startswith(current.arguments):
                    raise invalid_model_frame()
                current.arguments = text
                current.seal()
            else:
                text = _text(arguments)
                if text and (self.finished or current.closed):
                    raise invalid_model_frame()
                current.arguments += text

    def finish(self) -> tuple[WireTool, ...]:
        result = [self.calls[index] for index in sorted(self.calls)]
        # Validate every identity before classifying incomplete arguments: an
        # early empty call cannot hide a later slot's permanent protocol error.
        for current in result:
            current.seal()
        for current in result:
            # Missing/empty input cannot acquire {} execution authority.
            # Nonempty bad JSON stays available for nonexecutable feedback in
            # the original ModelPort/normalizer; no repair happens here.
            if not current.arguments:
                raise StudioError("MODEL_RESPONSE_INCOMPLETE", "Model tool input was not completed", status_code=502)
        if self.calls and not self.finished:
            raise StudioError("MODEL_RESPONSE_INCOMPLETE", "Model tool stream was not completed", status_code=502)
        return tuple(result)
