"""Host-owned optional retention of explicitly public tool output."""

from contextlib import contextmanager
from contextvars import ContextVar

_sink = ContextVar("ksadk_public_output_sink", default=None)


@contextmanager
def public_output_scope(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def retain_public_output(text: str, *, incomplete: bool = False) -> str | None:
    sink = _sink.get()
    if sink is None:
        return None
    try:
        return sink(text, incomplete=incomplete)
    except Exception:
        # Presentation storage cannot change execution or cause a tool retry.
        return None


def current_public_output_sink():
    return _sink.get()
