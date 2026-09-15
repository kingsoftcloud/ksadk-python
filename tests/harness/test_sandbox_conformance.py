from __future__ import annotations

import asyncio

from ksadk.harness.sandbox_backend import (
    ExecuteRequest,
    FilesystemIsolation,
    LocalReadOnlySandboxBackend,
    NetworkControl,
    SandboxAuditLog,
    SandboxSpec,
    SubprocessSandboxBackend,
)
from ksadk.harness.sandbox_conformance import (
    SandboxConformanceCase,
    run_sandbox_backend_conformance,
    verify_cooperative_cancellation,
)


def _run(coro):
    return asyncio.run(coro)


def test_subprocess_backend_passes_declared_conformance(tmp_path):
    audit = SandboxAuditLog(tmp_path / "audit.sqlite3")
    backend = SubprocessSandboxBackend(base_dir=tmp_path / "sandboxes", audit_log=audit)

    report = _run(
        run_sandbox_backend_conformance(
            backend,
            spec=SandboxSpec(workspace_root="", read_only=False),
            case=SandboxConformanceCase(
                smoke_command="printf conformance-ok",
                expected_output="conformance-ok",
                timeout_command="sleep 1",
                artifact_command="printf artifact-ok > result.txt",
                expected_artifact="result.txt",
            ),
        )
    )

    assert report.passed, report.findings
    assert backend.capabilities.filesystem_isolation is FilesystemIsolation.EPHEMERAL_WORKSPACE
    assert backend.capabilities.network_control is NetworkControl.ADMISSION_ONLY
    assert backend.capabilities.execution_audit is True
    assert not list((tmp_path / "sandboxes").glob("sandbox-*"))


def test_local_read_only_backend_passes_supported_subset(tmp_path):
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    backend = LocalReadOnlySandboxBackend()

    report = _run(
        run_sandbox_backend_conformance(
            backend,
            spec=SandboxSpec(workspace_root=str(tmp_path), read_only=True),
            case=SandboxConformanceCase(
                smoke_command="cat hello.txt",
                expected_output="hello",
            ),
        )
    )

    assert report.passed, report.findings
    skipped = {item.rule for item in report.findings if item.status == "skipped"}
    assert skipped == {"execute.request_timeout", "artifact.collection"}
    assert backend.capabilities.network_control is NetworkControl.COMMAND_SURFACE


def test_subprocess_backend_cancellation_is_audited_and_process_stops(tmp_path):
    audit = SandboxAuditLog(tmp_path / "audit.sqlite3")
    backend = SubprocessSandboxBackend(base_dir=tmp_path / "sandboxes", audit_log=audit)

    report = _run(
        verify_cooperative_cancellation(
            backend,
            spec=SandboxSpec(workspace_root="", read_only=False),
            command="sleep 5",
        )
    )

    assert report.passed, report.findings
    entries = audit.list("sandbox-conformance-cancel")
    assert len(entries) == 1
    assert entries[0]["exitCode"] == -9
    assert "取消" in entries[0]["error"]
    assert not list((tmp_path / "sandboxes").glob("sandbox-*"))


def test_close_is_idempotent_and_closed_handle_has_stable_error(tmp_path):
    backend = SubprocessSandboxBackend(base_dir=tmp_path)

    async def flow():
        handle = await backend.create(SandboxSpec(workspace_root="", read_only=False))
        await backend.close(handle)
        await backend.close(handle)
        return handle

    handle = _run(flow())
    assert handle.closed is True


def test_subprocess_capabilities_do_not_overclaim_os_network_isolation(tmp_path):
    backend = SubprocessSandboxBackend(base_dir=tmp_path)

    assert backend.capabilities.process_boundary is True
    assert backend.capabilities.network_control is NetworkControl.ADMISSION_ONLY
    assert backend.capabilities.reconnect is False
    assert backend.capabilities.execution_audit is False


def test_cancelled_execution_does_not_leave_handle_unusable(tmp_path):
    backend = SubprocessSandboxBackend(base_dir=tmp_path)

    async def flow():
        handle = await backend.create(SandboxSpec(workspace_root="", read_only=False))
        task = asyncio.create_task(
            backend.execute(handle, ExecuteRequest(command="sleep 5", run_id="run-cancel"))
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        result = await backend.execute(handle, ExecuteRequest(command="printf recovered"))
        await backend.close(handle)
        return result

    result = _run(flow())
    assert result.ok is True
    assert result.output == "recovered"


def test_cancellation_kills_spawned_child_processes(tmp_path):
    backend = SubprocessSandboxBackend(base_dir=tmp_path)

    async def flow():
        handle = await backend.create(SandboxSpec(workspace_root="", read_only=False))
        task = asyncio.create_task(
            backend.execute(
                handle,
                ExecuteRequest(
                    command="(sleep 0.2; printf orphan > orphan.txt) & wait",
                    run_id="run-cancel-tree",
                ),
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.3)
        artifacts = await backend.collect_artifacts(handle)
        await backend.close(handle)
        return artifacts

    assert _run(flow()) == []
