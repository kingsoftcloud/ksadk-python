"""Request-local tool item lifecycles for Studio's native wire decoders."""

import json
from dataclasses import dataclass, field

from ksadk.studio.model_wire_identity import invalid_model_frame


def validate_client_item_type(kind: str, *, wire_api: str) -> None:
    """Unsupported provider effects cannot be silently dropped as client prose."""
    hosted = (
        kind in {
            "web_search_call", "web_search_preview_call", "file_search_call",
            "code_interpreter_call", "computer_use_call", "image_generation_call",
            "mcp_call", "local_shell_call",
        } if wire_api == "responses"
        else kind == "server_tool_use" or kind.endswith("_tool_result")
    )
    if hosted:
        raise invalid_model_frame()


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise invalid_model_frame()
    return value


def _text(value: object) -> str:
    if type(value) is not str:
        raise invalid_model_frame()
    return value


def _identity(value: object) -> str:
    result = _text(value)
    if not result.strip():
        raise invalid_model_frame()
    return result


def _index(event: dict, name: str, *, required: bool = False) -> int | None:
    if name not in event:
        if required:
            raise invalid_model_frame()
        return None
    value = event[name]
    if type(value) is not int or value < 0:
        raise invalid_model_frame()
    return value


@dataclass
class WireTool:
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    closed: bool = False
    arguments_done: bool = False

    def pin(self, item: dict, *, id_name: str) -> None:
        for source, target in ((id_name, "call_id"), ("name", "name")):
            if source not in item:
                continue
            value = _identity(item[source])
            previous = getattr(self, target)
            if previous and previous != value:
                raise invalid_model_frame()
            setattr(self, target, value)

    def seal(self) -> None:
        _identity(self.call_id)
        _identity(self.name)
        self.closed = True


@dataclass
class ResponsesItems:
    """One shared slot namespace for message, reasoning and client tool items."""

    indexes: dict[int, str] = field(default_factory=dict)
    item_indexes: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)

    def pin(self, event: dict, item: dict | None = None, *, kind: str = "") -> str:
        item_id = _identity(item.get("id")) if item is not None else _identity(event.get("item_id"))
        if "item_id" in event and _identity(event["item_id"]) != item_id:
            raise invalid_model_frame()
        index = _index(event, "output_index")
        if index is not None:
            if self.indexes.get(index, item_id) != item_id:
                raise invalid_model_frame()
            if self.item_indexes.get(item_id, index) != index:
                raise invalid_model_frame()
            self.indexes[index] = item_id
            self.item_indexes[item_id] = index
        if item is not None:
            kind = _identity(item.get("type"))
        if kind:
            if self.kinds.get(item_id, kind) != kind:
                raise invalid_model_frame()
            self.kinds[item_id] = kind
        return item_id


@dataclass
class ResponsesToolCalls:
    calls: dict[str, WireTool] = field(default_factory=dict)
    items: ResponsesItems = field(default_factory=ResponsesItems)

    def _key(self, event: dict, item: dict | None = None) -> str:
        return self.items.pin(event, item, kind="function_call")

    def accept(self, event: dict) -> WireTool | None:
        kind = event["type"]
        if kind in {"response.output_item.added", "response.output_item.done"}:
            item = _object(event.get("item"))
            item_type = _identity(item.get("type"))
            validate_client_item_type(item_type, wire_api="responses")
            self.items.pin(event, item)
            if item_type != "function_call":
                return None
            key = self._key(event, item)
            current = self.calls.get(key)
            if kind == "response.output_item.added":
                if current is not None:
                    raise invalid_model_frame()
                current = WireTool()
                current.pin(item, id_name="call_id")
                if "arguments" in item:
                    current.arguments = _text(item["arguments"])
                self.calls[key] = current
                return None
            # A complete standalone done snapshot remains compatible. Deltas
            # still require a previously declared item, never a fabricated slot.
            if current is None:
                current = WireTool()
                self.calls[key] = current
            current.pin(item, id_name="call_id")
            if "arguments" in item:
                arguments = _text(item["arguments"])
                if (current.closed or current.arguments_done) and arguments != current.arguments:
                    raise invalid_model_frame()
                current.arguments = arguments
            if current.closed:
                return None
            current.seal()
            return current
        if kind in {
            "response.function_call_arguments.delta", "response.function_call_arguments.done",
        }:
            key = self._key(event)
            current = self.calls.get(key)
            if current is None or current.closed or current.arguments_done:
                raise invalid_model_frame()
            if kind.endswith(".delta"):
                current.arguments += _text(event.get("delta"))
            else:
                current.arguments = _text(event.get("arguments"))
                current.arguments_done = True
        return None

    def finish(self) -> None:
        if any(not tool.closed for tool in self.calls.values()):
            raise invalid_model_frame()

    def reconcile_output(self, output: list[dict]) -> tuple[WireTool, ...]:
        """Complete snapshots may fill gaps, never remove or rewrite closed calls."""
        seen = set()
        completed = []
        for index, item in enumerate(output):
            if item.get("type") != "function_call":
                continue
            key = _identity(item.get("id"))
            if key in seen:
                raise invalid_model_frame()
            seen.add(key)
            current = self.calls.get(key)
            arguments = _text(item.get("arguments"))
            if current is not None and not current.closed and current.arguments:
                if not arguments.startswith(current.arguments):
                    raise invalid_model_frame()
            tool = self.accept({"type": "response.output_item.done", "item": item,
                                "output_index": index})
            if tool is not None:
                completed.append(tool)
        if self.calls.keys() != seen:
            raise invalid_model_frame()
        self.finish()
        return tuple(completed)


@dataclass
class AnthropicToolBlocks:
    calls: dict[int, WireTool] = field(default_factory=dict)
    block_types: dict[int, str] = field(default_factory=dict)
    closed: set[int] = field(default_factory=set)
    initial_inputs: set[int] = field(default_factory=set)
    implicit_indexes: set[int] = field(default_factory=set)

    def accept(self, event: dict) -> None:
        kind = event["type"]
        if kind == "content_block_start":
            block = _object(event.get("content_block"))
            block_type = _identity(block.get("type"))
            validate_client_item_type(block_type, wire_api="anthropic")
            index = _index(event, "index", required=True)
            if index is not None:
                if index in self.block_types or index in self.closed:
                    raise invalid_model_frame()
                self.block_types[index] = block_type
            for name in ("text", "thinking", "signature"):
                if name in block:
                    _text(block[name])
            if block_type in {"text", "thinking"}:
                _text(block.get(block_type))
            if block_type == "redacted_thinking":
                _text(block.get("data"))
            if block_type == "tool_use":
                tool = WireTool()
                tool.pin(block, id_name="id")
                _identity(tool.call_id)
                _identity(tool.name)
                if "input" in block:
                    initial = _object(block["input"])
                    if initial:
                        tool.arguments = json.dumps(initial, ensure_ascii=False)
                        self.initial_inputs.add(index)
                self.calls[index] = tool
        elif kind == "content_block_delta":
            delta = _object(event.get("delta"))
            delta_type = _identity(delta.get("type"))
            index = _index(event, "index")
            if index is None and delta_type != "input_json_delta":
                # A legacy proxy may omit the block index on text/thinking
                # deltas; slot 0 stays compatible with that omission. Tool
                # argument deltas always carry their block identity.
                index = 0
            if index in self.closed:
                raise invalid_model_frame()
            expected = {"text_delta": "text", "thinking_delta": "thinking",
                        "signature_delta": "thinking", "citations_delta": "text",
                        "input_json_delta": "tool_use"}.get(delta_type)
            previous = self.block_types.get(index)
            if expected is not None:
                if previous is not None and previous != expected:
                    raise invalid_model_frame()
                if previous is None and expected in {"text", "thinking"}:
                    # Compatibility for a legacy proxy omitting block_start:
                    # the first typed non-executable delta still pins the slot.
                    self.block_types[index] = expected
                    self.implicit_indexes.add(index)
                field_name = {"text_delta": "text", "thinking_delta": "thinking",
                              "signature_delta": "signature"}.get(delta_type)
                if field_name is not None:
                    _text(delta.get(field_name))
            if delta_type == "input_json_delta":
                current = self.calls.get(index)
                if current is None or index in self.initial_inputs:
                    raise invalid_model_frame()
                current.arguments += _text(delta.get("partial_json"))
            elif index in self.calls:
                # Text/thinking cannot replace an already declared tool input.
                raise invalid_model_frame()
            for name in ("text", "thinking", "signature"):
                if name in delta:
                    _text(delta[name])
        elif kind == "content_block_stop":
            index = _index(event, "index", required=True)
            if index in self.closed or index not in self.block_types:
                raise invalid_model_frame()
            current = self.calls.get(index)
            if current is not None:
                current.seal()
            self.closed.add(index)

    def finish(self) -> tuple[WireTool, ...]:
        if any(not tool.closed for tool in self.calls.values()):
            raise invalid_model_frame()
        if not self.block_types.keys() <= self.closed | self.implicit_indexes:
            raise invalid_model_frame()
        self.closed.update(self.implicit_indexes)
        return tuple(self.calls.values())
