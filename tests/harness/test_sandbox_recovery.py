from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from ksadk.harness.sandbox_backend import (
    ExecuteRequest,
    ExecuteResult,
    FilesystemIsolation,
    NetworkControl,
    SandboxBackendCapabilities,
    SandboxCommandResumeToken,
    SandboxResumeToken,
    SandboxSpec,
)
from ksadk.harness.sandbox_recovery import (
    RecoverableSandboxCommandCoordinator,
    SandboxCommandJournalConflict,
    SandboxCommandJournalRecord,
    SandboxCommandJournalScope,
    SandboxCommandJournalState,
    SandboxCommandJournalUnavailable,
    complete_journal_record,
)


def _run(coro):
    return asyncio.run(coro)


class _WorkerCrash(BaseException):
    pass


_SCOPE = SandboxCommandJournalScope(
    tenant_id="tenant-1",
    workspace_id="workspace-1",
)


def _coordinator(adapter, journal, *, scope=_SCOPE):
    return RecoverableSandboxCommandCoordinator(adapter, journal, scope=scope)


@dataclass
class _Handle:
    spec: SandboxSpec
    handle_id: str = "sandbox-1"


@dataclass
class _Command:
    sandbox: _Handle
    request: ExecuteRequest


class _FakeRecoverableAdapter:
    capabilities = SandboxBackendCapabilities(
        backend_id="fake-remote",
        filesystem_isolation=FilesystemIsolation.REMOTE_SANDBOX,
        network_control=NetworkControl.ENFORCED,
        process_boundary=True,
        request_timeout=True,
        cooperative_cancellation=True,
        artifact_collection=True,
        deterministic_cleanup=True,
        execution_audit=True,
        reconnect=True,
        ownership_fencing=True,
        command_reconnect=True,
    )

    def __init__(self, *, crash_on_wait: bool = False, fail_on_wait: bool = False) -> None:
        self.crash_on_wait = crash_on_wait
        self.fail_on_wait = fail_on_wait
        self.cancelled = False
        self.reconnect_calls = 0
        self.detached = False
        self.cancel_wait = False

    async def start_execute(self, handle, request):
        return _Command(handle, request)

    def export_resume_token(self, handle):
        return SandboxResumeToken("fake-remote", handle.handle_id, "session-1")

    def export_command_resume_token(self, command):
        return SandboxCommandResumeToken(
            "fake-remote",
            command.sandbox.handle_id,
            "session-1",
            42,
        )

    async def wait_command(self, command):
        if self.cancel_wait:
            self.cancelled = True
            raise asyncio.CancelledError
        if self.crash_on_wait:
            self.crash_on_wait = False
            raise _WorkerCrash
        if self.fail_on_wait:
            raise RuntimeError("vendor temporarily unavailable")
        return ExecuteResult(ok=True, output="private-result", exit_code=0)

    async def cancel_command(self, command):
        self.cancelled = True
        return ExecuteResult(ok=False, output="", exit_code=130, error="cancelled")

    async def reconnect(self, token, *, spec):
        self.reconnect_calls += 1
        return _Handle(spec, handle_id=token.handle_id)

    async def reconnect_command(self, handle, token, *, request):
        assert token.process_id == 42
        return _Command(handle, request)

    async def detach(self, handle):
        self.detached = True


class _MemoryJournal:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.records: dict[str, SandboxCommandJournalRecord] = {}
        self.fail_create = fail_create

    async def create_running(self, record):
        if self.fail_create:
            raise OSError("control plane unavailable")
        if record.operation_id in self.records:
            raise SandboxCommandJournalConflict("duplicate")
        self.records[record.operation_id] = record
        return record

    async def load(self, operation_id, *, scope):
        record = self.records.get(operation_id)
        if record is None or record.scope != scope:
            return None
        return record

    async def finish(
        self,
        operation_id,
        *,
        scope,
        expected_version,
        state,
        exit_code,
    ):
        record = self.records[operation_id]
        if record.scope != scope:
            raise SandboxCommandJournalConflict("scope mismatch")
        if record.version != expected_version:
            raise SandboxCommandJournalConflict("stale version")
        completed = complete_journal_record(record, state=state, exit_code=exit_code)
        self.records[operation_id] = completed
        return completed


def _spec() -> SandboxSpec:
    return SandboxSpec(
        workspace_root="/workspace/revision-1",
        read_only=False,
        env={"PRIVATE_TOKEN": "must-not-enter-journal"},
    )


def _request() -> ExecuteRequest:
    return ExecuteRequest(
        command="process --secret must-not-enter-journal",
        timeout_seconds=30,
        run_id="run-1",
    )


def test_start_persists_credential_free_record_and_finishes():
    adapter = _FakeRecoverableAdapter()
    journal = _MemoryJournal()
    coordinator = _coordinator(adapter, journal)

    result = _run(
        coordinator.start_and_wait(
            _Handle(_spec()),
            _request(),
            operation_id="operation-1",
            execution_ref="agent-revision://finance@2#sandbox-step-1",
        )
    )

    record = journal.records["operation-1"]
    payload = str(record.to_dict())
    assert result.ok is True
    assert record.state is SandboxCommandJournalState.SUCCEEDED
    assert record.version == 2
    assert record.exit_code == 0
    assert "must-not-enter-journal" not in payload
    assert "process --secret" not in payload
    assert "PRIVATE_TOKEN" not in payload
    assert record.scope == _SCOPE


def test_journal_create_failure_cancels_untracked_command():
    adapter = _FakeRecoverableAdapter()
    coordinator = _coordinator(
        adapter,
        _MemoryJournal(fail_create=True),
    )

    with pytest.raises(SandboxCommandJournalUnavailable, match="fail-closed"):
        _run(
            coordinator.start_and_wait(
                _Handle(_spec()),
                _request(),
                operation_id="operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
            )
        )

    assert adapter.cancelled is True


def test_worker_crash_then_second_worker_recovers_and_finishes():
    journal = _MemoryJournal()
    first = _FakeRecoverableAdapter(crash_on_wait=True)
    first_coordinator = _coordinator(first, journal)

    with pytest.raises(_WorkerCrash):
        _run(
            first_coordinator.start_and_wait(
                _Handle(_spec()),
                _request(),
                operation_id="operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
            )
        )
    assert journal.records["operation-1"].state is SandboxCommandJournalState.RUNNING

    second = _FakeRecoverableAdapter()
    recovered = _run(
        _coordinator(second, journal).recover_and_wait(
            "operation-1",
            execution_ref="agent-revision://finance@2#sandbox-step-1",
            spec=_spec(),
            request=_request(),
        )
    )

    assert second.reconnect_calls == 1
    assert recovered.sandbox.handle_id == "sandbox-1"
    assert recovered.result.output == "private-result"
    assert recovered.journal.state is SandboxCommandJournalState.SUCCEEDED
    assert journal.records["operation-1"].version == 2


def test_recovery_rejects_changed_policy_before_remote_reconnect():
    journal = _MemoryJournal()
    first = _FakeRecoverableAdapter(crash_on_wait=True)
    with pytest.raises(_WorkerCrash):
        _run(
            _coordinator(first, journal).start_and_wait(
                _Handle(_spec()),
                _request(),
                operation_id="operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
            )
        )

    changed_spec = SandboxSpec(
        workspace_root="/different-workspace",
        read_only=False,
        env={"PRIVATE_TOKEN": "rotated-secret-is-allowed"},
    )
    second = _FakeRecoverableAdapter()
    with pytest.raises(SandboxCommandJournalConflict, match="Sandbox 策略"):
        _run(
            _coordinator(second, journal).recover_and_wait(
                "operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
                spec=changed_spec,
                request=_request(),
            )
        )
    assert second.reconnect_calls == 0


def test_journal_wire_round_trip_and_terminal_validation():
    journal = _MemoryJournal()
    first = _FakeRecoverableAdapter(crash_on_wait=True)
    with pytest.raises(_WorkerCrash):
        _run(
            _coordinator(first, journal).start_and_wait(
                _Handle(_spec()),
                _request(),
                operation_id="operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
            )
        )
    record = journal.records["operation-1"]
    assert SandboxCommandJournalRecord.from_dict(record.to_dict()) == record

    invalid = record.to_dict()
    invalid["state"] = "succeeded"
    with pytest.raises(ValueError, match="终态 Journal"):
        SandboxCommandJournalRecord.from_dict(invalid)


def test_failed_recovery_detaches_without_killing_remote_command():
    journal = _MemoryJournal()
    first = _FakeRecoverableAdapter(crash_on_wait=True)
    with pytest.raises(_WorkerCrash):
        _run(
            _coordinator(first, journal).start_and_wait(
                _Handle(_spec()),
                _request(),
                operation_id="operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
            )
        )

    second = _FakeRecoverableAdapter(fail_on_wait=True)
    with pytest.raises(RuntimeError, match="vendor temporarily unavailable"):
        _run(
            _coordinator(second, journal).recover_and_wait(
                "operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
                spec=_spec(),
                request=_request(),
            )
        )
    assert second.detached is True
    assert journal.records["operation-1"].state is SandboxCommandJournalState.RUNNING


def test_recovery_cannot_read_journal_from_another_scope():
    journal = _MemoryJournal()
    first = _FakeRecoverableAdapter(crash_on_wait=True)
    with pytest.raises(_WorkerCrash):
        _run(
            _coordinator(first, journal).start_and_wait(
                _Handle(_spec()),
                _request(),
                operation_id="operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
            )
        )

    other_scope = SandboxCommandJournalScope(
        tenant_id="tenant-2",
        workspace_id="workspace-1",
    )
    second = _FakeRecoverableAdapter()
    with pytest.raises(KeyError, match="Journal 不存在"):
        _run(
            _coordinator(second, journal, scope=other_scope).recover_and_wait(
                "operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
                spec=_spec(),
                request=_request(),
            )
        )
    assert second.reconnect_calls == 0


def test_secret_value_rotation_does_not_change_public_policy_fingerprint():
    journal = _MemoryJournal()
    first = _FakeRecoverableAdapter(crash_on_wait=True)
    with pytest.raises(_WorkerCrash):
        _run(
            _coordinator(first, journal).start_and_wait(
                _Handle(_spec()),
                _request(),
                operation_id="operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
            )
        )

    rotated_spec = SandboxSpec(
        workspace_root="/workspace/revision-1",
        read_only=False,
        env={"PRIVATE_TOKEN": "rotated-secret"},
    )
    second = _FakeRecoverableAdapter()
    recovered = _run(
        _coordinator(second, journal).recover_and_wait(
            "operation-1",
            execution_ref="agent-revision://finance@2#sandbox-step-1",
            spec=rotated_spec,
            request=_request(),
        )
    )

    assert recovered.result.ok is True
    assert recovered.journal.state is SandboxCommandJournalState.SUCCEEDED


def test_cancelled_recovery_marks_journal_terminal_and_detaches():
    journal = _MemoryJournal()
    first = _FakeRecoverableAdapter(crash_on_wait=True)
    with pytest.raises(_WorkerCrash):
        _run(
            _coordinator(first, journal).start_and_wait(
                _Handle(_spec()),
                _request(),
                operation_id="operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
            )
        )

    second = _FakeRecoverableAdapter()
    second.cancel_wait = True
    with pytest.raises(asyncio.CancelledError):
        _run(
            _coordinator(second, journal).recover_and_wait(
                "operation-1",
                execution_ref="agent-revision://finance@2#sandbox-step-1",
                spec=_spec(),
                request=_request(),
            )
        )

    record = journal.records["operation-1"]
    assert record.state is SandboxCommandJournalState.CANCELLED
    assert record.exit_code == 130
    assert second.detached is True
