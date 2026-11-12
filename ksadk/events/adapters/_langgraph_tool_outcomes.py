"""Retain typed tool outcomes before LangGraph v3 serializes errors to text."""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphInterrupt


class NativeToolOutcomes(BaseCallbackHandler):
    """Invocation-local evidence keyed exactly like the public tools stream.

    Native v3 loses the exception type in ``tool-error.message``. Public tool
    callbacks still retain it. Queue *all* terminal outcomes, so a later pause
    cannot reclassify an earlier failure when a call ID is reused.
    """

    run_inline = True

    def __init__(self):
        self._lock = Lock()
        self._calls: dict[str, tuple[tuple[str, ...], str]] = {}
        self._outcomes: dict[tuple[tuple[str, ...], str], deque] = defaultdict(deque)

    def on_tool_start(self, _serialized, _input, *, run_id, metadata=None, **kwargs):
        if not isinstance(metadata, dict):
            return
        checkpoint_ns = metadata.get("langgraph_checkpoint_ns") or ""
        if not isinstance(checkpoint_ns, str):
            return
        namespace = tuple(checkpoint_ns.split("|"))[:-1] if checkpoint_ns else ()
        call_id = str(kwargs.get("tool_call_id") or run_id)
        with self._lock:
            self._calls[str(run_id)] = (namespace, call_id)

    def on_tool_end(self, _output, *, run_id, **_kwargs):
        self._terminal(run_id, "tool-finished", None)

    def on_tool_error(self, error: BaseException, *, run_id, **_kwargs):
        self._terminal(run_id, "tool-error", error)

    def _terminal(self, run_id, kind, error):
        with self._lock:
            key = self._calls.pop(str(run_id), None)
            if key is not None:
                self._outcomes[key].append((kind, error))

    def consume(self, namespace: tuple[str, ...], call_id: str, kind: str) -> bool:
        """Only a matching typed GraphInterrupt proves control-flow pause."""
        with self._lock:
            key = (namespace, call_id)
            outcomes = self._outcomes.get(key)
            if not outcomes:
                return False
            observed_kind, error = outcomes.popleft()
            if not outcomes:
                del self._outcomes[key]
        return observed_kind == kind == "tool-error" and isinstance(error, GraphInterrupt)


def with_tool_outcomes(config: dict[str, Any]):
    """Preserve user callbacks without changing their list or manager."""
    from langchain_core.callbacks.manager import AsyncCallbackManager

    observer = NativeToolOutcomes()
    manager = AsyncCallbackManager.configure(inheritable_callbacks=config.get("callbacks"))
    manager.add_handler(observer, inherit=True)
    return {**config, "callbacks": manager}, observer
