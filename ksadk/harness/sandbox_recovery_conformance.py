"""Conformance suite for authoritative Sandbox command journals.

The provider itself belongs to the control plane.  This module gives provider
implementations a reusable, storage-agnostic acceptance suite so an HTTP or DB
adapter cannot silently weaken tenant isolation, optimistic concurrency, or
terminal-state immutability.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ksadk.harness.sandbox_backend import (
    SandboxCommandResumeToken,
    SandboxResumeToken,
)
from ksadk.harness.sandbox_recovery import (
    SandboxCommandJournalConflict,
    SandboxCommandJournalProvider,
    SandboxCommandJournalRecord,
    SandboxCommandJournalScope,
    SandboxCommandJournalState,
)


@dataclass(frozen=True)
class SandboxCommandJournalConformanceFinding:
    rule: str
    status: str
    detail: str = ""


@dataclass
class SandboxCommandJournalConformanceReport:
    findings: list[SandboxCommandJournalConformanceFinding] = field(
        default_factory=list
    )

    @property
    def passed(self) -> bool:
        return not any(item.status == "failed" for item in self.findings)

    def pass_rule(self, rule: str) -> None:
        self.findings.append(
            SandboxCommandJournalConformanceFinding(rule, "passed")
        )

    def fail_rule(self, rule: str, detail: str) -> None:
        self.findings.append(
            SandboxCommandJournalConformanceFinding(rule, "failed", detail)
        )


async def run_sandbox_command_journal_conformance(
    provider: SandboxCommandJournalProvider,
    *,
    scope: SandboxCommandJournalScope,
    operation_id: str,
) -> SandboxCommandJournalConformanceReport:
    """Verify one provider with an isolated, caller-owned operation id.

    The suite intentionally has no delete operation: authoritative journals
    are audit records.  Callers should use a dedicated test tenant/workspace
    and a unique ``operation_id`` for every run.
    """

    report = SandboxCommandJournalConformanceReport()
    record = _probe_record(scope=scope, operation_id=operation_id)

    try:
        created = await provider.create_running(record)
    except Exception as exc:  # noqa: BLE001 - conformance reports provider failures
        report.fail_rule("journal.create_running", _exception_detail(exc))
        return report
    if created == record:
        report.pass_rule("journal.create_running")
    else:
        report.fail_rule("journal.create_running", "provider changed the wire record")

    await _verify_create_only(provider, record, report)
    await _verify_scope_isolation(provider, record, report)

    loaded = await _safe_load(provider, record, report)
    if loaded is None:
        return report

    completed = await _verify_terminal_transition(provider, loaded, report)
    if completed is None:
        return report
    await _verify_stale_write_rejected(provider, loaded, report)
    await _verify_terminal_immutable(provider, completed, report)
    await _verify_terminal_load(provider, completed, report)
    return report


async def _verify_create_only(provider, record, report) -> None:
    try:
        await provider.create_running(record)
    except SandboxCommandJournalConflict:
        report.pass_rule("journal.create_only")
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("journal.create_only", _exception_detail(exc))
    else:
        report.fail_rule("journal.create_only", "duplicate create was accepted")


async def _verify_scope_isolation(provider, record, report) -> None:
    other_scope = SandboxCommandJournalScope(
        tenant_id=f"{record.scope.tenant_id}-other",
        workspace_id=record.scope.workspace_id,
    )
    try:
        leaked = await provider.load(record.operation_id, scope=other_scope)
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("journal.scope_isolation", _exception_detail(exc))
        return
    if leaked is None:
        report.pass_rule("journal.scope_isolation")
    else:
        report.fail_rule(
            "journal.scope_isolation",
            "record was visible from another tenant scope",
        )


async def _safe_load(provider, record, report):
    try:
        loaded = await provider.load(record.operation_id, scope=record.scope)
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("journal.load_round_trip", _exception_detail(exc))
        return None
    if loaded == record:
        report.pass_rule("journal.load_round_trip")
        return loaded
    report.fail_rule("journal.load_round_trip", "stored record did not round-trip")
    return None


async def _verify_terminal_transition(provider, record, report):
    try:
        completed = await provider.finish(
            record.operation_id,
            scope=record.scope,
            expected_version=record.version,
            state=SandboxCommandJournalState.SUCCEEDED,
            exit_code=0,
        )
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("journal.finish_cas", _exception_detail(exc))
        return None
    if (
        completed.state is SandboxCommandJournalState.SUCCEEDED
        and completed.exit_code == 0
        and completed.version == record.version + 1
        and completed.scope == record.scope
    ):
        report.pass_rule("journal.finish_cas")
        return completed
    report.fail_rule("journal.finish_cas", "terminal fields/version are invalid")
    return None


async def _verify_stale_write_rejected(provider, running, report) -> None:
    try:
        await provider.finish(
            running.operation_id,
            scope=running.scope,
            expected_version=running.version,
            state=SandboxCommandJournalState.FAILED,
            exit_code=1,
        )
    except SandboxCommandJournalConflict:
        report.pass_rule("journal.stale_write_rejected")
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("journal.stale_write_rejected", _exception_detail(exc))
    else:
        report.fail_rule("journal.stale_write_rejected", "stale CAS write was accepted")


async def _verify_terminal_immutable(provider, completed, report) -> None:
    try:
        await provider.finish(
            completed.operation_id,
            scope=completed.scope,
            expected_version=completed.version,
            state=SandboxCommandJournalState.FAILED,
            exit_code=1,
        )
    except SandboxCommandJournalConflict:
        report.pass_rule("journal.terminal_immutable")
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("journal.terminal_immutable", _exception_detail(exc))
    else:
        report.fail_rule("journal.terminal_immutable", "terminal record was overwritten")


async def _verify_terminal_load(provider, completed, report) -> None:
    try:
        loaded = await provider.load(completed.operation_id, scope=completed.scope)
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("journal.terminal_round_trip", _exception_detail(exc))
        return
    if loaded == completed:
        report.pass_rule("journal.terminal_round_trip")
    else:
        report.fail_rule("journal.terminal_round_trip", "terminal record did not persist")


def _probe_record(
    *,
    scope: SandboxCommandJournalScope,
    operation_id: str,
) -> SandboxCommandJournalRecord:
    return SandboxCommandJournalRecord(
        operation_id=operation_id,
        scope=scope,
        execution_ref="agent-revision://journal-conformance@1#probe",
        run_id=f"journal-conformance:{operation_id}",
        sandbox_token=SandboxResumeToken(
            backend_id="conformance",
            handle_id=f"handle:{operation_id}",
            session_locator=f"session:{operation_id}",
        ),
        command_token=SandboxCommandResumeToken(
            backend_id="conformance",
            handle_id=f"handle:{operation_id}",
            session_locator=f"session:{operation_id}",
            process_id=1,
        ),
        spec_fingerprint="sha256:" + "1" * 64,
        request_fingerprint="sha256:" + "2" * 64,
        timeout_seconds=30,
        started_at=time.time(),
    )


def _exception_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "SandboxCommandJournalConformanceFinding",
    "SandboxCommandJournalConformanceReport",
    "run_sandbox_command_journal_conformance",
]
