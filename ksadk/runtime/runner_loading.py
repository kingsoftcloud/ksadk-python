"""Lazy framework-runner initialization at the RuntimeAdapter boundary."""

from __future__ import annotations

from typing import Any


def ensure_runner_loaded(runner: Any, *, runtime_type: str) -> None:
    """Load a runner at first real execution, never during app bootstrap.

    ``RuntimeRegistry.create()`` is also used by capability/bootstrap paths;
    loading there would execute user code merely for rendering Studio. The
    first ``start`` is the shared execution boundary for HTTP, A2A and Studio,
    so loader errors are still returned before a stream body is opened.
    """

    if bool(getattr(runner, "_ksadk_runtime_loaded", False)):
        return
    # A caller may deliberately inject an already-constructed framework agent
    # (for example a durable restore or an embedding application). Respect that
    # ownership and do not re-import its entrypoint.
    if getattr(runner, "_agent", None) is not None:
        setattr(runner, "_ksadk_runtime_loaded", True)
        return
    load_agent = getattr(runner, "load_agent", None)
    if not callable(load_agent):
        # Runtime factories require BaseRunner, but lightweight adapters used by
        # an external integration may already be executable and expose no
        # separate initialization hook. There is nothing to defer in that case.
        setattr(runner, "_ksadk_runtime_loaded", True)
        return
    load_agent()
    setattr(runner, "_ksadk_runtime_loaded", True)


__all__ = ["ensure_runner_loaded"]
