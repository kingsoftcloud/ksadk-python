"""回合级 tool_approval_mode 对 run.approval_required 的覆盖。"""
from __future__ import annotations

from types import SimpleNamespace

from ksadk.harness.engine.policy_runtime import _apply_request_approval_mode


def _run():
    return SimpleNamespace(
        approval_required={"write_workspace_file"},
        tools={"write_workspace_file": object()},
        execution_policy_request=None,
    )


def test_full_mode_clears_approval_required():
    run = _run()
    request = SimpleNamespace(metadata={"tool_approval_mode": "full"})
    _apply_request_approval_mode(run, request)
    assert run.approval_required == set()


def test_ask_mode_requires_everything():
    run = _run()
    request = SimpleNamespace(metadata={"tool_approval_mode": "ask"})
    _apply_request_approval_mode(run, request)
    assert run.approval_required == {"write_workspace_file"}


def test_default_keeps_static_contracts():
    run = _run()
    _apply_request_approval_mode(run, SimpleNamespace(metadata={}))
    assert run.approval_required == {"write_workspace_file"}


def test_falls_back_to_run_execution_policy_request():
    run = _run()
    run.execution_policy_request = SimpleNamespace(metadata={"tool_approval_mode": "full"})
    _apply_request_approval_mode(run, None)
    assert run.approval_required == set()
