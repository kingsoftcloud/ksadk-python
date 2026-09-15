from __future__ import annotations

import asyncio

from ksadk.harness.sandbox_recovery import (
    SandboxCommandJournalConflict,
    SandboxCommandJournalScope,
    complete_journal_record,
)
from ksadk.harness.sandbox_recovery_conformance import (
    run_sandbox_command_journal_conformance,
)


def _run(coro):
    return asyncio.run(coro)


class _CompliantJournal:
    def __init__(self) -> None:
        self.records = {}

    async def create_running(self, record):
        key = (record.scope, record.operation_id)
        if key in self.records:
            raise SandboxCommandJournalConflict("duplicate")
        self.records[key] = record
        return record

    async def load(self, operation_id, *, scope):
        return self.records.get((scope, operation_id))

    async def finish(
        self,
        operation_id,
        *,
        scope,
        expected_version,
        state,
        exit_code,
    ):
        key = (scope, operation_id)
        record = self.records[key]
        if record.state.terminal or record.version != expected_version:
            raise SandboxCommandJournalConflict("stale or terminal")
        completed = complete_journal_record(
            record,
            state=state,
            exit_code=exit_code,
        )
        self.records[key] = completed
        return completed


class _LeakyScopeJournal(_CompliantJournal):
    async def load(self, operation_id, *, scope):
        return next(
            (
                record
                for (_, stored_operation_id), record in self.records.items()
                if stored_operation_id == operation_id
            ),
            None,
        )


class _MutableTerminalJournal(_CompliantJournal):
    async def finish(
        self,
        operation_id,
        *,
        scope,
        expected_version,
        state,
        exit_code,
    ):
        key = (scope, operation_id)
        record = self.records[key]
        completed = complete_journal_record(
            record,
            state=state,
            exit_code=exit_code,
        )
        self.records[key] = completed
        return completed


_SCOPE = SandboxCommandJournalScope("tenant-conformance", "workspace-conformance")


def test_compliant_provider_passes_all_journal_rules():
    report = _run(
        run_sandbox_command_journal_conformance(
            _CompliantJournal(),
            scope=_SCOPE,
            operation_id="operation-compliant",
        )
    )

    assert report.passed, report.findings
    assert {item.rule for item in report.findings} == {
        "journal.create_running",
        "journal.create_only",
        "journal.scope_isolation",
        "journal.load_round_trip",
        "journal.finish_cas",
        "journal.stale_write_rejected",
        "journal.terminal_immutable",
        "journal.terminal_round_trip",
    }


def test_conformance_detects_cross_tenant_read():
    report = _run(
        run_sandbox_command_journal_conformance(
            _LeakyScopeJournal(),
            scope=_SCOPE,
            operation_id="operation-leaky",
        )
    )

    assert report.passed is False
    finding = next(
        item for item in report.findings if item.rule == "journal.scope_isolation"
    )
    assert finding.status == "failed"


def test_conformance_detects_stale_and_terminal_overwrites():
    report = _run(
        run_sandbox_command_journal_conformance(
            _MutableTerminalJournal(),
            scope=_SCOPE,
            operation_id="operation-mutable",
        )
    )

    assert report.passed is False
    failures = {item.rule for item in report.findings if item.status == "failed"}
    assert failures == {
        "journal.stale_write_rejected",
        "journal.terminal_immutable",
        "journal.terminal_round_trip",
    }
