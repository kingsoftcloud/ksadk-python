"""Stable SDK result contract for control-plane command reconciliation.

The control plane owns stale-record discovery, scheduling, attempt limits and
operator workflows.  KsADK reconciles exactly one authoritative Journal record
and reports a credential-free disposition; it never scans a local database or
guesses that an unreachable remote process has failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ksadk.harness.sandbox_backend import ExecuteRequest, SandboxSpec
from ksadk.harness.sandbox_recovery import (
    RecoverableSandboxCommandCoordinator,
    RecoveredSandboxCommandExecution,
    SandboxCommandAlreadyTerminal,
    SandboxCommandJournalNotFound,
    SandboxCommandJournalRecord,
    SandboxCommandJournalUnavailable,
    SandboxCommandRecoveryInputMismatch,
)


class SandboxCommandReconciliationStatus(str, Enum):
    """Control-plane action implied by one reconciliation attempt."""

    RECOVERED = "recovered"
    ALREADY_TERMINAL = "already_terminal"
    NOT_FOUND = "not_found"
    RETRY_LATER = "retry_later"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class SandboxCommandReconciliationResult:
    """Credential-free outcome safe to persist in control-plane audit data."""

    operation_id: str
    status: SandboxCommandReconciliationStatus
    reason_code: str
    journal: SandboxCommandJournalRecord | None = None
    execution: RecoveredSandboxCommandExecution | None = None

    @property
    def retryable(self) -> bool:
        return self.status is SandboxCommandReconciliationStatus.RETRY_LATER

    @property
    def requires_operator(self) -> bool:
        return self.status in {
            SandboxCommandReconciliationStatus.NOT_FOUND,
            SandboxCommandReconciliationStatus.MANUAL_REVIEW,
        }

    def to_dict(self) -> dict[str, object]:
        """Project the stable audit payload without live handles or errors."""

        return {
            "schemaVersion": 1,
            "operationId": self.operation_id,
            "status": self.status.value,
            "reasonCode": self.reason_code,
            "retryable": self.retryable,
            "requiresOperator": self.requires_operator,
            "journalState": self.journal.state.value if self.journal else None,
            "journalVersion": self.journal.version if self.journal else None,
        }


class SandboxCommandReconciler:
    """Reconcile one record selected and reconstructed by the control plane."""

    def __init__(self, coordinator: RecoverableSandboxCommandCoordinator) -> None:
        self._coordinator = coordinator

    async def reconcile(
        self,
        operation_id: str,
        *,
        execution_ref: str,
        spec: SandboxSpec,
        request: ExecuteRequest,
    ) -> SandboxCommandReconciliationResult:
        try:
            execution = await self._coordinator.recover_and_wait(
                operation_id,
                execution_ref=execution_ref,
                spec=spec,
                request=request,
            )
        except SandboxCommandAlreadyTerminal as exc:
            return SandboxCommandReconciliationResult(
                operation_id=operation_id,
                status=SandboxCommandReconciliationStatus.ALREADY_TERMINAL,
                reason_code=f"journal_{exc.record.state.value}",
                journal=exc.record,
            )
        except SandboxCommandJournalNotFound:
            return SandboxCommandReconciliationResult(
                operation_id=operation_id,
                status=SandboxCommandReconciliationStatus.NOT_FOUND,
                reason_code="journal_not_found",
            )
        except SandboxCommandRecoveryInputMismatch as exc:
            return SandboxCommandReconciliationResult(
                operation_id=operation_id,
                status=SandboxCommandReconciliationStatus.MANUAL_REVIEW,
                reason_code=exc.reason_code,
            )
        except SandboxCommandJournalUnavailable:
            return SandboxCommandReconciliationResult(
                operation_id=operation_id,
                status=SandboxCommandReconciliationStatus.RETRY_LATER,
                reason_code="journal_unavailable",
            )
        except (ConnectionError, OSError, TimeoutError, RuntimeError):
            # Do not persist vendor error text: it may contain endpoints or
            # request material. The authoritative command remains RUNNING and
            # a later fenced worker may safely retry.
            return SandboxCommandReconciliationResult(
                operation_id=operation_id,
                status=SandboxCommandReconciliationStatus.RETRY_LATER,
                reason_code="sandbox_backend_unavailable",
            )
        return SandboxCommandReconciliationResult(
            operation_id=operation_id,
            status=SandboxCommandReconciliationStatus.RECOVERED,
            reason_code=f"journal_{execution.journal.state.value}",
            journal=execution.journal,
            execution=execution,
        )


__all__ = [
    "SandboxCommandReconciler",
    "SandboxCommandReconciliationResult",
    "SandboxCommandReconciliationStatus",
]
