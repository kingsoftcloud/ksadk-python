"""Request-local content admission for Chat and Anthropic streams."""

import copy
from dataclasses import dataclass, field

from ksadk._model_json import load_model_json
from ksadk._model_reasoning import chat_reasoning
from ksadk.studio.model_wire_identity import invalid_model_frame
from ksadk.studio.model_wire_tools import AnthropicToolBlocks, _object, _text


def _reasoning(message: dict) -> str:
    try:
        return chat_reasoning(message)
    except ValueError:
        raise invalid_model_frame()


@dataclass
class ChatContent:
    text: str = ""
    reasoning: str = ""
    finished: bool = False
    snapshots: set[str] = field(default_factory=set)

    def accept(self, delta: dict, *messages: dict) -> tuple[str, str]:
        before_text, before_reasoning = self.text, self.reasoning
        additions = {"text": delta.get("content"), "reasoning": _reasoning(delta)}
        for name, value in additions.items():
            if value is None:
                continue
            _text(value)
            if value and (self.finished or name in self.snapshots):
                raise invalid_model_frame()
            setattr(self, name, getattr(self, name) + value)
        for message in messages:
            values = {}
            if message.get("content") is not None:
                values["text"] = _text(message["content"])
            if any(message.get(key) is not None for key in ("reasoning_content", "reasoning")):
                values["reasoning"] = _reasoning(message)
            for name, value in values.items():
                previous = getattr(self, name)
                if (name in self.snapshots and value != previous) or not value.startswith(previous):
                    raise invalid_model_frame()
                setattr(self, name, value)
                self.snapshots.add(name)
        return self.text[len(before_text):], self.reasoning[len(before_reasoning):]


@dataclass
class AnthropicContent:
    blocks: AnthropicToolBlocks
    values: dict[int, str] = field(default_factory=dict)
    emitted_text: str = ""
    emitted_reasoning: str = ""
    finished: bool = False
    originals: dict[int, dict] = field(default_factory=dict)

    def _prefix(self, kind: str) -> str:
        pieces = []
        for expected, (index, block_type) in enumerate(sorted(self.blocks.block_types.items())):
            if index != expected:
                break
            if block_type != kind:
                continue
            pieces.append(self.values.get(index, ""))
            if index not in self.blocks.closed:
                break
        return "".join(pieces)

    def _drain(self) -> tuple[str, str]:
        text, reasoning = self._prefix("text"), self._prefix("thinking")
        if (not text.startswith(self.emitted_text)
                or not reasoning.startswith(self.emitted_reasoning)):
            raise invalid_model_frame()
        result = text[len(self.emitted_text):], reasoning[len(self.emitted_reasoning):]
        self.emitted_text, self.emitted_reasoning = text, reasoning
        return result

    def accept(self, event: dict) -> tuple[str, str]:
        kind = event["type"]
        if self.finished and kind in {"content_block_start", "content_block_delta"}:
            raise invalid_model_frame()
        if kind == "content_block_start":
            block = _object(event.get("content_block"))
            block_type = block["type"]
            self.originals[event["index"]] = copy.deepcopy(block)
            if block_type in {"text", "thinking"}:
                self.values[event["index"]] = _text(block.get(block_type))
        elif kind == "content_block_delta":
            delta = _object(event.get("delta"))
            field_name = {"text_delta": "text", "thinking_delta": "thinking"}.get(delta["type"])
            if field_name is not None:
                # A legacy proxy may omit the block index on text/thinking
                # deltas; slot 0 stays compatible with that omission.
                index = event["index"] if "index" in event else 0
                self.values[index] = self.values.get(index, "") + _text(delta.get(field_name))
            if delta["type"] == "signature_delta":
                index = event["index"] if "index" in event else 0
                original = self.originals.setdefault(index, {"type": "thinking"})
                original["signature"] = original.get("signature", "") + _text(delta["signature"])
        return self._drain()

    def finish(self) -> tuple[str, str]:
        self.blocks.finish()
        indexes = sorted(self.blocks.block_types)
        if any(index != expected for expected, index in enumerate(indexes)):
            raise invalid_model_frame()
        return self._drain()

    def replay_content(self) -> tuple[dict, ...] | None:
        self.finish()
        values = []
        for index, kind in sorted(self.blocks.block_types.items()):
            block = copy.deepcopy(self.originals.get(index, {"type": kind}))
            if kind in {"text", "thinking"}:
                block[kind] = self.values.get(index, "")
            elif kind == "tool_use":
                try:
                    block["input"] = load_model_json(self.blocks.calls[index].arguments or "{}")
                except (ValueError, TypeError):
                    # Invalid JSON has no complete content to replay. Retain
                    # the tool diagnostic without fabricating original input.
                    return None
            values.append(block)
        # Transport keeps opaque provider fields; the declared Native replay
        # contract validates signed content before authorizing any tool batch.
        return tuple(values)
