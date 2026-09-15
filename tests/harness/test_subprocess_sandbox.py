"""缺口 6：进程隔离 Sandbox 后端 + 执行审计持久化。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.sandbox_backend import (
    ExecuteRequest,
    SandboxAuditLog,
    SandboxPolicyViolation,
    SandboxSpec,
    SubprocessSandboxBackend,
)


def _run(coro):
    return asyncio.run(coro)


def test_execute_writes_and_collects_artifacts(tmp_path):
    audit = SandboxAuditLog(tmp_path / "audit.sqlite3")
    backend = SubprocessSandboxBackend(base_dir=tmp_path / "sandboxes", audit_log=audit)

    async def flow():
        handle = await backend.create(SandboxSpec(workspace_root="", read_only=False))
        result = await backend.execute(
            handle, ExecuteRequest(command="echo hi > out.txt && cat out.txt", run_id="run-1")
        )
        assert result.ok and "hi" in result.output
        artifacts = await backend.collect_artifacts(handle)
        assert artifacts == ["out.txt"]
        content = await backend.read_artifact(handle, "out.txt")
        assert content == b"hi\n"
        await backend.close(handle)

    _run(flow())
    # 审计留痕（run_id 锚定）。
    entries = audit.list("run-1")
    assert len(entries) == 1
    assert entries[0]["command"].startswith("echo hi")
    assert entries[0]["ok"] is True or entries[0]["ok"] == 1
    assert entries[0]["exitCode"] == 0
    # close 销毁一次性工作区。
    assert not list((tmp_path / "sandboxes").glob("sandbox-*"))


def test_network_egress_policy_rejected(tmp_path):
    backend = SubprocessSandboxBackend(base_dir=tmp_path)
    with pytest.raises(SandboxPolicyViolation):
        _run(backend.create(SandboxSpec(workspace_root="", network_egress=("api.example.com",))))


def test_timeout_enforced_and_audited(tmp_path):
    audit = SandboxAuditLog(tmp_path / "audit.sqlite3")
    backend = SubprocessSandboxBackend(base_dir=tmp_path, audit_log=audit)

    async def flow():
        handle = await backend.create(SandboxSpec(workspace_root="", read_only=False))
        result = await backend.execute(
            handle, ExecuteRequest(command="sleep 5", timeout_seconds=0.2, run_id="run-2")
        )
        await backend.close(handle)
        return result

    result = _run(flow())
    assert not result.ok and result.exit_code == -9
    entries = audit.list("run-2")
    assert entries and entries[0]["ok"] in (0, False)
    assert "超时" in entries[0]["error"]


def test_env_scrubbed(tmp_path):
    import os

    os.environ["SANDBOX_TEST_SECRET"] = "leaky"

    backend = SubprocessSandboxBackend(base_dir=tmp_path)

    async def flow():
        handle = await backend.create(SandboxSpec(workspace_root="", read_only=False))
        result = await backend.execute(
            handle,
            ExecuteRequest(command="echo $SANDBOX_TEST_SECRET; echo sentinel-ok"),
        )
        await backend.close(handle)
        return result

    result = _run(flow())
    assert "sentinel-ok" in result.output
    assert "leaky" not in result.output
    del os.environ["SANDBOX_TEST_SECRET"]


def test_output_truncated(tmp_path):
    backend = SubprocessSandboxBackend(base_dir=tmp_path, max_output_bytes=100)

    async def flow():
        handle = await backend.create(SandboxSpec(workspace_root="", read_only=False))
        result = await backend.execute(
            handle, ExecuteRequest(command="python3 -c \"print('x'*1000)\"")
        )
        await backend.close(handle)
        return result

    result = _run(flow())
    assert len(result.output) < 200
    assert "截断" in result.output
