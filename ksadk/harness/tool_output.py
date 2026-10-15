"""Optional host output observation, separate from tool execution authority.

All ToolPort.execute wrappers inherit the observation context. A host may
publish stdout/stderr after its existing admission checks; observers cannot
approve, retry, complete or cancel tools. Raw chunks never enter UI events.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Literal

from .tool_display import display_text, tool_display_output, tool_display_start

OutputObserver = Callable[[str, str], None]
_OBSERVER: ContextVar[OutputObserver | None] = ContextVar("ksadk_public_tool_output", default=None)


@contextmanager
def tool_output_scope(observer: OutputObserver) -> Iterator[None]:
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def current_tool_output_observer() -> OutputObserver | None:
    return _OBSERVER.get()


def publish_tool_output(channel: str, text: str) -> None:
    observer = _OBSERVER.get()
    if observer is not None and channel in {"stdout", "stderr"} and isinstance(text, str):
        try:
            observer(channel, text)
        except Exception:
            # Presentation must never change execution/receipt outcomes.
            pass


@dataclass(frozen=True)
class ToolOutputObservation:
    kind: Literal["tool_output"]
    operation_key: str
    call_id: str
    display: dict


class PublicOutputBuffer:
    """Publish bounded complete lines so secrets split across chunks stay private."""

    def __init__(self, name: str, args: dict, emit: Callable[[dict], None]):
        self.start = tool_display_start(name, args)
        self.emit = emit
        self.pending = {"stdout": "", "stderr": ""}
        self.visible = ""
        self.total_chars = 0
        self.truncated = False
        self.lock = RLock()
        self.last_publish = 0.0
        self.published_chars = 0
        self.publications = 0
        self.closed = False
        self.private_key = {"stdout": False, "stderr": False}

    def observe(self, channel: str, text: str) -> None:
        with self.lock:
            self._observe(channel, text)

    def _observe(self, channel: str, text: str) -> None:
        if self.closed or channel not in self.pending or not isinstance(text, str):
            return
        self.total_chars += len(text)
        if self.total_chars > 32768:
            self.truncated = True
        # Buffer each unfinished line separately; a credential may span chunks.
        self.pending[channel] = (self.pending[channel] + text)[:32768]
        lines = self.pending[channel].splitlines(keepends=True)
        self.pending[channel] = ""
        for line in lines:
            if line.endswith(("\n", "\r")):
                self._line(channel, line)
            else:
                self.pending[channel] = line
        self._publish()

    def _line(self, channel: str, line: str) -> None:
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            self.private_key[channel] = True
            line = "[redacted key]\n"
        elif self.private_key[channel]:
            if "-----END " in line and "PRIVATE KEY-----" in line:
                self.private_key[channel] = False
            return
        if len(self.visible) >= 16384:
            self.truncated = True
            return
        clean = display_text(line, 32768)
        remaining = 16384 - len(self.visible)
        if len(clean) > remaining:
            self.truncated = True
        self.visible += clean[:remaining]

    def _publish(self, *, final: bool = False) -> None:
        now = monotonic()
        if not self.visible and not self.truncated:
            return
        if not final and (
            self.publications >= 63
            or (
                self.publications
                and now - self.last_publish < 0.2
                and len(self.visible) - self.published_chars < 1024
            )
        ):
            return
        display = tool_display_output(self.start, output=self.visible)
        display.update(truncated=self.truncated, total_chars=self.total_chars)
        self.last_publish = now
        self.published_chars = len(self.visible)
        self.publications += 1
        try:
            self.emit(display)
        except Exception:
            # Observation failure cannot replace a tool result or cancellation.
            pass

    def finish(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for channel in self.pending:
                if self.pending[channel]:
                    self._line(channel, self.pending[channel])
                    self.pending[channel] = ""
            self._publish(final=True)
