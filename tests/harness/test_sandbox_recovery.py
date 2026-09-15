from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import pytest

from ksadk.harness.sandbox_adapters import adapt_e2b_backend
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
from ksadk.harness.sandbox_lease import (
    SandboxLeaseConflict,
    SandboxLeaseGrant,
    SandboxLeaseScope,
)
from ksadk.harness.sandbox_reconciliation import (
    SandboxCommandReconciler,
    SandboxCommandReconciliationStatus,
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
from ksadk.sandbox.backends.e2b import E2BSandboxBackend
from ksadk.sandbox.base import SandboxSpec as SdkSandboxSpec


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
    def __init__(
        self,
        *,
        fail_create: bool = False,
        fail_finish: bool = False,
    ) -> None:
        self.records: dict[str, SandboxCommandJournalRecord] = {}
        self.fail_create = fail_create
        self.fail_finish = fail_finish

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
        if self.fail_finish:
            raise OSError("control plane unavailable")
        record = self.records[operation_id]
        if record.scope != scope:
            raise SandboxCommandJournalConflict("scope mismatch")
        if record.version != expected_version:
            raise SandboxCommandJournalConflict("stale version")
        completed = complete_journal_record(record, state=state, exit_code=exit_code)
        self.records[operation_id] = completed
        return completed


class _CancelledCreateJournal(_MemoryJournal):
    async def create_running(self, record):
        raise asyncio.CancelledError


class _MemoryLeaseProvider:
    """Process-shared lease double for the gated remote-backend E2E.

    The remote command and reconnect are real E2B operations.  The lease
    remains an SDK-side test provider because its authoritative production
    implementation belongs to agentengine-server.
    """

    def __init__(self) -> None:
        self.now = 100.0
        self._grants: dict[tuple[SandboxLeaseScope, str], SandboxLeaseGrant] = {}
        self._last_fencing: dict[tuple[SandboxLeaseScope, str], int] = {}

    async def acquire(
        self,
        *,
        backend_id,
        handle_id,
        scope,
        owner_id,
        ttl_seconds,
    ):
        key = (scope, handle_id)
        current = self._grants.get(key)
        if current is not None and current.expires_at > self.now:
            raise SandboxLeaseConflict("sandbox lease already owned")
        fencing = self._last_fencing.get(key, 0) + 1
        grant = SandboxLeaseGrant(
            backend_id=backend_id,
            handle_id=handle_id,
            scope=scope,
            owner_id=owner_id,
            fencing_token=fencing,
            expires_at=self.now + ttl_seconds,
        )
        self._last_fencing[key] = fencing
        self._grants[key] = grant
        return grant

    async def renew(self, grant, *, ttl_seconds):
        key = (grant.scope, grant.handle_id)
        current = self._grants.get(key)
        if (
            current is None
            or current.owner_id != grant.owner_id
            or current.fencing_token != grant.fencing_token
            or current.expires_at <= self.now
        ):
            raise SandboxLeaseConflict("stale sandbox fencing token")
        renewed = SandboxLeaseGrant(
            backend_id=grant.backend_id,
            handle_id=grant.handle_id,
            scope=grant.scope,
            owner_id=grant.owner_id,
            fencing_token=grant.fencing_token,
            expires_at=self.now + ttl_seconds,
        )
        self._grants[key] = renewed
        return renewed

    async def release(self, grant):
        key = (grant.scope, grant.handle_id)
        current = self._grants.get(key)
        if (
            current is None
            or current.owner_id != grant.owner_id
            or current.fencing_token != grant.fencing_token
        ):
            raise SandboxLeaseConflict("stale sandbox fencing token")
        self._grants.pop(key)

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


def test_start_exposes_durable_recovery_boundary_before_wait():
    adapter = _FakeRecoverableAdapter()
    journal = _MemoryJournal()
    coordinator = _coordinator(adapter, journal)

    started = _run(
        coordinator.start(
            _Handle(_spec()),
            _request(),
            operation_id="operation-1",
            execution_ref="agent-revision://finance@2#sandbox-step-1",
        )
    )

    assert started.journal is journal.records["operation-1"]
    assert started.journal.state is SandboxCommandJournalState.RUNNING
    assert started.journal.command_token.process_id == 42

    result = _run(coordinator.wait(started))
    assert result.ok is True
    assert journal.records["operation-1"].state is SandboxCommandJournalState.SUCCEEDED


def test_second_worker_can_recover_after_first_worker_only_started_command():
    journal = _MemoryJournal()
    first = _FakeRecoverableAdapter()
    started = _run(
        _coordinator(first, journal).start(
            _Handle(_spec()),
            _request(),
            operation_id="operation-1",
            execution_ref="agent-revision://finance@2#sandbox-step-1",
        )
    )
    assert started.journal.state is SandboxCommandJournalState.RUNNING

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
    assert recovered.result.output == "private-result"
    assert recovered.journal.state is SandboxCommandJournalState.SUCCEEDED


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


def test_cancellation_during_journal_create_cancels_untracked_command():
    adapter = _FakeRecoverableAdapter()
    coordinator = _coordinator(adapter, _CancelledCreateJournal())

    with pytest.raises(asyncio.CancelledError):
        _run(
            coordinator.start(
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


def _running_journal(*, fail_finish: bool = False) -> _MemoryJournal:
    journal = _MemoryJournal(fail_finish=fail_finish)
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
    return journal


def _reconcile(adapter, journal, *, spec=None):
    return _run(
        SandboxCommandReconciler(_coordinator(adapter, journal)).reconcile(
            "operation-1",
            execution_ref="agent-revision://finance@2#sandbox-step-1",
            spec=spec or _spec(),
            request=_request(),
        )
    )


def test_reconciler_recovers_and_reports_terminal_journal():
    journal = _running_journal()

    result = _reconcile(_FakeRecoverableAdapter(), journal)

    assert result.status is SandboxCommandReconciliationStatus.RECOVERED
    assert result.reason_code == "journal_succeeded"
    assert result.retryable is False
    assert result.requires_operator is False
    assert result.execution is not None
    assert result.journal is journal.records["operation-1"]
    assert result.to_dict() == {
        "schemaVersion": 1,
        "operationId": "operation-1",
        "status": "recovered",
        "reasonCode": "journal_succeeded",
        "retryable": False,
        "requiresOperator": False,
        "journalState": "succeeded",
        "journalVersion": 2,
    }


def test_reconciler_treats_existing_terminal_record_as_idempotent_success():
    journal = _running_journal()
    _reconcile(_FakeRecoverableAdapter(), journal)
    second = _FakeRecoverableAdapter()

    result = _reconcile(second, journal)

    assert result.status is SandboxCommandReconciliationStatus.ALREADY_TERMINAL
    assert result.reason_code == "journal_succeeded"
    assert result.journal is journal.records["operation-1"]
    assert second.reconnect_calls == 0


def test_reconciler_reports_missing_record_for_operator_review():
    result = _reconcile(_FakeRecoverableAdapter(), _MemoryJournal())

    assert result.status is SandboxCommandReconciliationStatus.NOT_FOUND
    assert result.reason_code == "journal_not_found"
    assert result.requires_operator is True


def test_reconciler_does_not_retry_changed_immutable_policy():
    journal = _running_journal()
    changed_spec = SandboxSpec(
        workspace_root="/different-workspace",
        read_only=False,
        env={"PRIVATE_TOKEN": "rotated-secret"},
    )
    adapter = _FakeRecoverableAdapter()

    result = _reconcile(adapter, journal, spec=changed_spec)

    assert result.status is SandboxCommandReconciliationStatus.MANUAL_REVIEW
    assert result.reason_code == "spec_fingerprint_mismatch"
    assert result.requires_operator is True
    assert adapter.reconnect_calls == 0
    assert journal.records["operation-1"].state is SandboxCommandJournalState.RUNNING


def test_reconciler_retries_backend_failure_without_finishing_journal():
    journal = _running_journal()

    result = _reconcile(_FakeRecoverableAdapter(fail_on_wait=True), journal)

    assert result.status is SandboxCommandReconciliationStatus.RETRY_LATER
    assert result.reason_code == "sandbox_backend_unavailable"
    assert result.retryable is True
    assert journal.records["operation-1"].state is SandboxCommandJournalState.RUNNING


def test_reconciler_retries_when_terminal_journal_write_is_unavailable():
    journal = _running_journal(fail_finish=True)

    result = _reconcile(_FakeRecoverableAdapter(), journal)

    assert result.status is SandboxCommandReconciliationStatus.RETRY_LATER
    assert result.reason_code == "journal_unavailable"
    assert result.retryable is True
    assert journal.records["operation-1"].state is SandboxCommandJournalState.RUNNING


def test_real_e2b_worker_takeover_finishes_authoritative_journal_when_enabled():
    """Exercise a real remote command across two adapter/worker instances.

    E2B supplies the remote Sandbox and process reconnect. The in-memory lease
    and journal stand in only for agentengine-server contracts, so this test
    does not claim to validate server durability.
    """

    if os.environ.get("KSADK_REAL_SANDBOX_E2E") != "1":
        pytest.skip("set KSADK_REAL_SANDBOX_E2E=1 to run remote recovery E2E")
    template_id = os.environ.get("KSADK_SANDBOX_TEMPLATE_ID", "").strip()
    if not template_id:
        pytest.skip("KSADK_SANDBOX_TEMPLATE_ID is required for remote recovery E2E")

    sdk = E2BSandboxBackend(
        spec=SdkSandboxSpec(
            template_id=template_id,
            timeout=120,
            allow_internet_access=False,
        )
    )
    leases = _MemoryLeaseProvider()
    lease_scope = SandboxLeaseScope("tenant-e2e", "workspace-e2e")
    journal = _MemoryJournal()
    journal_scope = SandboxCommandJournalScope("tenant-e2e", "workspace-e2e")
    spec = SandboxSpec(
        workspace_root="/tmp/ksadk-harness-recovery-e2e",
        read_only=False,
    )
    request = ExecuteRequest(
        command="sleep 5; printf remote-worker-recovery-ok",
        timeout_seconds=30,
        run_id="run-remote-worker-recovery",
    )
    execution_ref = "agent-revision://sandbox-recovery-e2e@1#command-1"

    async def flow():
        first_adapter = adapt_e2b_backend(
            sdk,
            lease_provider=leases,
            lease_scope=lease_scope,
            lease_owner_id="worker-a",
            lease_ttl_seconds=5,
        )
        first_handle = await first_adapter.create(spec)
        first_coordinator = RecoverableSandboxCommandCoordinator(
            first_adapter,
            journal,
            scope=journal_scope,
        )
        started = await first_coordinator.start(
            first_handle,
            request,
            operation_id="operation-remote-worker-recovery",
            execution_ref=execution_ref,
        )
        assert started.journal.state is SandboxCommandJournalState.RUNNING

        # Simulate worker-a disappearing after the durable recovery boundary.
        # The virtual control-plane clock avoids a minute-long wall-clock wait.
        leases.advance(61)
        second_adapter = adapt_e2b_backend(
            sdk,
            lease_provider=leases,
            lease_scope=lease_scope,
            lease_owner_id="worker-b",
            lease_ttl_seconds=5,
        )
        second_coordinator = RecoverableSandboxCommandCoordinator(
            second_adapter,
            journal,
            scope=journal_scope,
        )
        recovered = await second_coordinator.recover_and_wait(
            "operation-remote-worker-recovery",
            execution_ref=execution_ref,
            spec=spec,
            request=request,
        )
        await second_adapter.close(recovered.sandbox)
        return first_handle, recovered

    first_handle, recovered = _run(flow())
    assert recovered.sandbox.handle_id == first_handle.handle_id
    assert recovered.result.ok is True
    assert recovered.result.output == "remote-worker-recovery-ok"
    assert recovered.journal.state is SandboxCommandJournalState.SUCCEEDED
    assert recovered.journal.version == 2
