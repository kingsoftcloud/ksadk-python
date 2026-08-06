"""Pinned production ``openai-codex`` API surface contract.

This is deliberately an introspection test against the installed wheel, not a
mock of :class:`AsyncCodexClient`.  The transport test exercises these methods
through a local JSON-RPC app-server; the separately gated E2E test exercises a
real account and bundled CLI process.
"""

from __future__ import annotations

import inspect
from importlib.metadata import version

import pytest

openai_codex = pytest.importorskip("openai_codex")


def test_pinned_openai_codex_thread_turn_surface() -> None:
    """KSADK only relies on methods provided by the exact pinned SDK."""
    from openai_codex import AsyncCodex, AsyncThread, AsyncTurnHandle

    assert version("openai-codex") == "0.144.4"

    for owner, method_name in (
        (AsyncCodex, "thread_start"),
        (AsyncCodex, "thread_resume"),
        (AsyncCodex, "close"),
        (AsyncThread, "turn"),
        (AsyncTurnHandle, "interrupt"),
    ):
        method = getattr(owner, method_name, None)
        assert callable(method), f"{owner.__name__}.{method_name} disappeared"
        assert inspect.iscoroutinefunction(method), f"{owner.__name__}.{method_name} must await"

    assert inspect.isasyncgenfunction(AsyncTurnHandle.stream)

    start = inspect.signature(AsyncCodex.thread_start)
    resume = inspect.signature(AsyncCodex.thread_resume)
    turn = inspect.signature(AsyncThread.turn)
    assert "ephemeral" in start.parameters
    assert "thread_id" in resume.parameters
    assert "input" in turn.parameters


def test_production_client_does_not_reference_retired_codex_api_names() -> None:
    """Keep the old fake API names from silently re-entering production code."""
    from pathlib import Path

    source_dir = Path(__file__).resolve().parents[2] / "ksadk" / "codex"
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_dir.glob("*.py"))
    for retired_name in (
        "run_turn_stream",
        "interrupt_turn",
        "kill_thread",
        "drain_pending_approvals",
    ):
        assert retired_name not in source
