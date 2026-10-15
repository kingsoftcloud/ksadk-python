"""Transform native LangGraph tool results for canonical replay."""

from __future__ import annotations

from typing import Any

from langgraph.stream import ProtocolEvent, StreamTransformer


class _CanonicalToolsTransformer(StreamTransformer):
    """Subscribe the canonical adapter to native tool execution events.

    The v3 default modes omit ``tools``. A call-site transformer requests
    that mode without changing the compiled graph or taking ownership of
    the user's ``tool_calls`` projection. Empty projections also coexist
    with configured ToolCallTransformer factories without key collisions.
    Native events pass through unchanged, including nested namespaces;
    only native tool-finished/tool-error events determine tool outcomes.
    """

    required_stream_modes = ("tools",)

    def init(self) -> dict[str, Any]:
        return {}

    def process(self, event: ProtocolEvent) -> bool:
        return True
