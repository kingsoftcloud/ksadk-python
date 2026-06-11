from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

ReasoningPartKind = Literal["thinking", "text"]


@dataclass(frozen=True)
class ReasoningMarkupPart:
    kind: ReasoningPartKind
    text: str


@dataclass
class ReasoningMarkupParser:
    """Incrementally split inline <think> markup out of streamed text."""

    _buffer: str = ""
    _in_think: bool = False
    _pending: list[ReasoningMarkupPart] = field(default_factory=list)

    def feed(self, chunk: str) -> list[ReasoningMarkupPart]:
        self._buffer += str(chunk or "")
        parts: list[ReasoningMarkupPart] = []

        while self._buffer:
            tag = "</think>" if self._in_think else "<think>"
            index = self._buffer.find(tag)
            if index >= 0:
                self._append(parts, self._buffer[:index])
                self._buffer = self._buffer[index + len(tag):]
                self._in_think = not self._in_think
                continue

            prefix_len = self._partial_tag_prefix_len(self._buffer, tag)
            if self._in_think:
                break
            emit_len = len(self._buffer) - prefix_len
            if emit_len <= 0:
                break
            self._append(parts, self._buffer[:emit_len])
            self._buffer = self._buffer[emit_len:]
            break

        return self._merge(parts)

    def flush(self) -> list[ReasoningMarkupPart]:
        if not self._buffer:
            return []
        parts: list[ReasoningMarkupPart] = []
        self._append(parts, self._buffer)
        self._buffer = ""
        return self._merge(parts)

    def _append(self, parts: list[ReasoningMarkupPart], text: str) -> None:
        if not text:
            return
        parts.append(ReasoningMarkupPart("thinking" if self._in_think else "text", text))

    @staticmethod
    def _merge(parts: list[ReasoningMarkupPart]) -> list[ReasoningMarkupPart]:
        merged: list[ReasoningMarkupPart] = []
        for part in parts:
            if not part.text:
                continue
            if merged and merged[-1].kind == part.kind:
                merged[-1] = ReasoningMarkupPart(part.kind, merged[-1].text + part.text)
            else:
                merged.append(part)
        return merged

    @staticmethod
    def _partial_tag_prefix_len(value: str, tag: str) -> int:
        max_len = min(len(value), len(tag) - 1)
        for size in range(max_len, 0, -1):
            if tag.startswith(value[-size:]):
                return size
        return 0


def strip_reasoning_markup(text: str) -> str:
    """Remove inline <think> blocks from text used as final answer or title input."""
    value = str(text or "")
    if not value:
        return ""
    value = re.sub(r"<think\b[^>]*>.*?</think>", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<think\b[^>]*>.*$", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"</?think\b[^>]*>", "", value, flags=re.IGNORECASE)
    return value
