"""Control-plane journal contract for recoverable Sandbox commands.

The authoritative implementation belongs to ``agentengine-server``.  KsADK
defines the credential-free wire record and coordinates the adapter calls; it
does not use a local file or SQLite database as a cloud recovery fact source.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol

from ksadk.harness.sandbox_adapters import (
    SessionSandboxBackendAdapter,
    SessionSandboxCommand,
    SessionSandboxHandle,
)
from ksadk.harness.sandbox_backend import (
    ExecuteRequest,
    ExecuteResult,
    SandboxCommandResumeToken,
    SandboxResumeToken,
    SandboxSpec,
)


class SandboxCommandJournalConflict(RuntimeError):
    """A create-only or optimistic-version journal write lost a race."""


class SandboxCommandJournalUnavailable(RuntimeError):
    """The authoritative journal could not durably accept an update."""


class SandboxCommandJournalNotFound(KeyError):
    """The scoped authoritative journal record does not exist."""


class SandboxCommandAlreadyTerminal(SandboxCommandJournalConflict):
    """Recovery raced with, or was requested after, terminal completion."""

    def __init__(self, record: SandboxCommandJournalRecord) -> None:
        self.record = record
        super().__init__(f"Sandbox 命令已处于终态: {record.state.value}")


class SandboxCommandRecoveryInputMismatch(SandboxCommandJournalConflict):
    """Immutable recovery input differs from the journal fact."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class SandboxCommandJournalState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self is not SandboxCommandJournalState.RUNNING


@dataclass(frozen=True)
class SandboxCommandJournalScope:
    """Tenant/workspace boundary for every journal mutation and lookup."""

    tenant_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.workspace_id.strip():
            raise ValueError("Journal tenant_id 和 workspace_id 不能为空")

    def to_dict(self) -> dict[str, str]:
        return {
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SandboxCommandJournalScope:
        return cls(
            tenant_id=str(value.get("tenantId", "")),
            workspace_id=str(value.get("workspaceId", "")),
        )


@dataclass(frozen=True)
class SandboxCommandJournalRecord:
    """Credential-free state needed to reconnect one in-flight command.

    ``execution_ref`` must identify an immutable Revision/Bundle operation in
    the control plane.  The command and environment are intentionally absent;
    recovery reconstructs them from that fact source and verifies the public
    policy fingerprint before reconnecting.
    """

    operation_id: str
    scope: SandboxCommandJournalScope
    execution_ref: str
    run_id: str
    sandbox_token: SandboxResumeToken
    command_token: SandboxCommandResumeToken
    spec_fingerprint: str
    request_fingerprint: str
    timeout_seconds: float
    started_at: float
    state: SandboxCommandJournalState = SandboxCommandJournalState.RUNNING
    version: int = 1
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if not self.operation_id.strip() or not self.execution_ref.strip():
            raise ValueError("operation_id 和 execution_ref 不能为空")
        if not self.run_id.strip():
            raise ValueError("run_id 不能为空")
        if self.timeout_seconds <= 0 or self.started_at <= 0 or self.version <= 0:
            raise ValueError("Journal 时间与版本字段必须大于 0")
        if not self.spec_fingerprint or not self.request_fingerprint:
            raise ValueError("Journal 缺少配置指纹")
        if self.state.terminal != (self.exit_code is not None):
            raise ValueError("终态 Journal 必须记录 exit_code，运行态不得记录")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "operationId": self.operation_id,
            "scope": self.scope.to_dict(),
            "executionRef": self.execution_ref,
            "runId": self.run_id,
            "sandboxToken": self.sandbox_token.to_dict(),
            "commandToken": self.command_token.to_dict(),
            "specFingerprint": self.spec_fingerprint,
            "requestFingerprint": self.request_fingerprint,
            "timeoutSeconds": self.timeout_seconds,
            "startedAt": self.started_at,
            "state": self.state.value,
            "version": self.version,
            "exitCode": self.exit_code,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SandboxCommandJournalRecord:
        if value.get("schemaVersion") != 1:
            raise ValueError("Sandbox Command Journal schemaVersion 不支持")
        try:
            state = SandboxCommandJournalState(str(value.get("state", "")))
            raw_exit_code = value.get("exitCode")
            return cls(
                operation_id=str(value.get("operationId", "")),
                scope=SandboxCommandJournalScope.from_dict(dict(value["scope"])),
                execution_ref=str(value.get("executionRef", "")),
                run_id=str(value.get("runId", "")),
                sandbox_token=SandboxResumeToken.from_dict(dict(value["sandboxToken"])),
                command_token=SandboxCommandResumeToken.from_dict(
                    dict(value["commandToken"])
                ),
                spec_fingerprint=str(value.get("specFingerprint", "")),
                request_fingerprint=str(value.get("requestFingerprint", "")),
                timeout_seconds=float(value.get("timeoutSeconds", 0)),
                started_at=float(value.get("startedAt", 0)),
                state=state,
                version=int(value.get("version", 0)),
                exit_code=None if raw_exit_code is None else int(raw_exit_code),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and "Journal" in str(exc):
                raise
            raise ValueError("Sandbox Command Journal 记录无效") from exc


class SandboxCommandJournalProvider(Protocol):
    """Authoritative durable journal implemented by the control plane.

    ``create_running`` is create-only and may return only after the record is
    durable. ``finish`` is optimistic: implementations must reject a stale
    ``expected_version`` and any attempt to overwrite a terminal record with
    :class:`SandboxCommandJournalConflict`. All reads and writes are scoped by
    tenant/workspace; a cross-scope load must behave as not found.
    """

    async def create_running(
        self,
        record: SandboxCommandJournalRecord,
    ) -> SandboxCommandJournalRecord: ...

    async def load(
        self,
        operation_id: str,
        *,
        scope: SandboxCommandJournalScope,
    ) -> SandboxCommandJournalRecord | None: ...

    async def finish(
        self,
        operation_id: str,
        *,
        scope: SandboxCommandJournalScope,
        expected_version: int,
        state: SandboxCommandJournalState,
        exit_code: int,
    ) -> SandboxCommandJournalRecord: ...


@dataclass(frozen=True)
class RecoveredSandboxCommandExecution:
    sandbox: SessionSandboxHandle
    result: ExecuteResult
    journal: SandboxCommandJournalRecord


@dataclass(frozen=True)
class StartedRecoverableSandboxCommand:
    """A live command whose reconnect tokens are already durable.

    The object is intentionally process-local because it contains a live
    adapter command handle.  A replacement worker reconstructs its state from
    ``journal`` through :meth:`recover_and_wait`, never by serializing this
    wrapper.
    """

    command: SessionSandboxCommand
    journal: SandboxCommandJournalRecord


class RecoverableSandboxCommandCoordinator:
    """Compose command reconnect primitives with an authoritative journal."""

    def __init__(
        self,
        adapter: SessionSandboxBackendAdapter,
        journal: SandboxCommandJournalProvider,
        *,
        scope: SandboxCommandJournalScope,
    ) -> None:
        if not adapter.capabilities.command_reconnect:
            raise ValueError("Recoverable coordinator 需要 command_reconnect 能力")
        self._adapter = adapter
        self._journal = journal
        self._scope = scope

    async def start_and_wait(
        self,
        handle: SessionSandboxHandle,
        request: ExecuteRequest,
        *,
        operation_id: str,
        execution_ref: str,
    ) -> ExecuteResult:
        started = await self.start(
            handle,
            request,
            operation_id=operation_id,
            execution_ref=execution_ref,
        )
        return await self.wait(started)

    async def start(
        self,
        handle: SessionSandboxHandle,
        request: ExecuteRequest,
        *,
        operation_id: str,
        execution_ref: str,
    ) -> StartedRecoverableSandboxCommand:
        """Start a command and durably publish its recovery tokens.

        Returning from this method is the recoverability boundary: callers
        may lose the current worker afterwards and a replacement worker can
        continue with :meth:`recover_and_wait`.
        """

        command = await self._adapter.start_execute(handle, request)
        record = self._build_running_record(
            command,
            operation_id=operation_id,
            execution_ref=execution_ref,
        )
        try:
            durable = await self._journal.create_running(record)
            self._validate_created_record(record, durable)
        except asyncio.CancelledError:
            # Task cancellation is a BaseException on supported Python
            # versions, so it must be handled separately from provider
            # failures. No durable token means the remote side effect must not
            # survive the cancelled worker.
            await asyncio.shield(self._adapter.cancel_command(command))
            raise
        except Exception as exc:
            # No durable token means no recoverability. Stop the remote process
            # rather than letting an untracked side effect continue.
            await self._adapter.cancel_command(command)
            raise SandboxCommandJournalUnavailable(
                "Sandbox 命令恢复记录未持久化，已 fail-closed 取消命令"
            ) from exc

        return StartedRecoverableSandboxCommand(command=command, journal=durable)

    async def wait(self, started: StartedRecoverableSandboxCommand) -> ExecuteResult:
        """Wait for a previously started command and persist its terminal state."""

        self._validate_started_command(started)

        try:
            result = await self._adapter.wait_command(started.command)
        except asyncio.CancelledError:
            await self._finish_cancelled(started.journal)
            raise
        await self._finish_result(started.journal, result)
        return result

    def _validate_started_command(self, started: StartedRecoverableSandboxCommand) -> None:
        command = started.command
        record = started.journal
        if record.scope != self._scope:
            raise SandboxCommandRecoveryInputMismatch(
                "scope_mismatch",
                "tenant/workspace 与已启动命令不匹配",
            )
        if record.state is not SandboxCommandJournalState.RUNNING:
            raise SandboxCommandAlreadyTerminal(record)
        if record.run_id != command.request.run_id:
            raise SandboxCommandRecoveryInputMismatch(
                "run_id_mismatch",
                "run_id 与已启动命令不匹配",
            )
        if record.sandbox_token != self._adapter.export_resume_token(command.sandbox):
            raise SandboxCommandRecoveryInputMismatch(
                "sandbox_token_mismatch",
                "Sandbox Token 与已启动命令不匹配",
            )
        if record.command_token != self._adapter.export_command_resume_token(command):
            raise SandboxCommandRecoveryInputMismatch(
                "command_token_mismatch",
                "Command Token 与已启动命令不匹配",
            )
        if record.spec_fingerprint != _spec_fingerprint(command.sandbox.spec):
            raise SandboxCommandRecoveryInputMismatch(
                "spec_fingerprint_mismatch",
                "Sandbox 策略与已启动命令不匹配",
            )
        if record.request_fingerprint != _request_fingerprint(
            execution_ref=record.execution_ref,
            request=command.request,
        ):
            raise SandboxCommandRecoveryInputMismatch(
                "request_fingerprint_mismatch",
                "执行请求与已启动命令不匹配",
            )

    async def recover_and_wait(
        self,
        operation_id: str,
        *,
        execution_ref: str,
        spec: SandboxSpec,
        request: ExecuteRequest,
    ) -> RecoveredSandboxCommandExecution:
        record = await self._journal.load(operation_id, scope=self._scope)
        if record is None:
            raise SandboxCommandJournalNotFound(
                f"Sandbox Command Journal 不存在: {operation_id}"
            )
        if record.state is not SandboxCommandJournalState.RUNNING:
            raise SandboxCommandAlreadyTerminal(record)
        self._validate_recovery_input(
            record,
            execution_ref=execution_ref,
            spec=spec,
            request=request,
        )
        sandbox = await self._adapter.reconnect(record.sandbox_token, spec=spec)
        try:
            command = await self._adapter.reconnect_command(
                sandbox,
                record.command_token,
                request=request,
            )
            result = await self._adapter.wait_command(command)
            completed = await self._finish_result(record, result)
        except asyncio.CancelledError:
            # ``wait_command`` cancels the remote process before propagating
            # cancellation. Keep the authoritative journal aligned with that
            # terminal outcome so reconciliation never retries a dead PID.
            await self._finish_cancelled(record)
            await self._release_recovery_handle(sandbox)
            raise
        except BaseException:
            # The acquired lease remains the authoritative fencing boundary.
            # Release this process' ownership without killing a command whose
            # result may still be recoverable by a later worker.
            await self._release_recovery_handle(sandbox)
            raise
        return RecoveredSandboxCommandExecution(
            sandbox=sandbox,
            result=result,
            journal=completed,
        )

    def _build_running_record(
        self,
        command: SessionSandboxCommand,
        *,
        operation_id: str,
        execution_ref: str,
    ) -> SandboxCommandJournalRecord:
        request = command.request
        return SandboxCommandJournalRecord(
            operation_id=operation_id,
            scope=self._scope,
            execution_ref=execution_ref,
            run_id=request.run_id,
            sandbox_token=self._adapter.export_resume_token(command.sandbox),
            command_token=self._adapter.export_command_resume_token(command),
            spec_fingerprint=_spec_fingerprint(command.sandbox.spec),
            request_fingerprint=_request_fingerprint(
                execution_ref=execution_ref,
                request=request,
            ),
            timeout_seconds=request.timeout_seconds,
            started_at=time.time(),
        )

    async def _finish_result(
        self,
        record: SandboxCommandJournalRecord,
        result: ExecuteResult,
    ) -> SandboxCommandJournalRecord:
        state = (
            SandboxCommandJournalState.SUCCEEDED
            if result.ok
            else SandboxCommandJournalState.FAILED
        )
        try:
            return await self._journal.finish(
                record.operation_id,
                scope=self._scope,
                expected_version=record.version,
                state=state,
                exit_code=result.exit_code,
            )
        except Exception as exc:
            raise SandboxCommandJournalUnavailable(
                "Sandbox 命令已结束，但 Journal 终态写入失败"
            ) from exc

    async def _finish_cancelled(self, record: SandboxCommandJournalRecord) -> None:
        try:
            await asyncio.shield(
                self._journal.finish(
                    record.operation_id,
                    scope=self._scope,
                    expected_version=record.version,
                    state=SandboxCommandJournalState.CANCELLED,
                    exit_code=130,
                )
            )
        except Exception:
            # Preserve task cancellation. The journal remains RUNNING and can
            # be reconciled by the control plane using the process token.
            return

    @staticmethod
    def _validate_created_record(
        expected: SandboxCommandJournalRecord,
        actual: SandboxCommandJournalRecord,
    ) -> None:
        if actual != expected:
            raise SandboxCommandJournalConflict(
                "Journal create_running 返回了不同的恢复记录"
            )

    def _validate_recovery_input(
        self,
        record: SandboxCommandJournalRecord,
        *,
        execution_ref: str,
        spec: SandboxSpec,
        request: ExecuteRequest,
    ) -> None:
        if record.execution_ref != execution_ref:
            raise SandboxCommandRecoveryInputMismatch(
                "execution_ref_mismatch",
                "execution_ref 与 Journal 不匹配",
            )
        if record.scope != self._scope:
            raise SandboxCommandRecoveryInputMismatch(
                "scope_mismatch",
                "tenant/workspace 与 Journal 不匹配",
            )
        if record.run_id != request.run_id:
            raise SandboxCommandRecoveryInputMismatch(
                "run_id_mismatch",
                "run_id 与 Journal 不匹配",
            )
        if record.spec_fingerprint != _spec_fingerprint(spec):
            raise SandboxCommandRecoveryInputMismatch(
                "spec_fingerprint_mismatch",
                "Sandbox 策略与 Journal 不匹配",
            )
        if record.request_fingerprint != _request_fingerprint(
            execution_ref=execution_ref,
            request=request,
        ):
            raise SandboxCommandRecoveryInputMismatch(
                "request_fingerprint_mismatch",
                "执行请求与 Journal 不匹配",
            )

    async def _release_recovery_handle(self, sandbox: SessionSandboxHandle) -> None:
        # ``close`` would kill the remote Sandbox. An unsuccessful reconnect
        # must only relinquish this worker's lease. This adapter-private call
        # is intentionally kept here until the platform lifecycle owns the
        # outer Sandbox cleanup operation.
        await self._adapter.detach(sandbox)


def _spec_fingerprint(spec: SandboxSpec) -> str:
    public_policy = {
        "workspaceRoot": spec.workspace_root,
        "readOnly": spec.read_only,
        "networkEgress": sorted(spec.network_egress),
        # Values are Secret material and may rotate between recovery attempts.
        "envKeys": sorted(spec.env),
    }
    return _digest(public_policy)


def _request_fingerprint(*, execution_ref: str, request: ExecuteRequest) -> str:
    # The immutable execution_ref is the command fact source. The command text
    # itself is deliberately not persisted or hashed into the journal.
    return _digest(
        {
            "executionRef": execution_ref,
            "runId": request.run_id,
            "timeoutSeconds": request.timeout_seconds,
        }
    )


def _digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def complete_journal_record(
    record: SandboxCommandJournalRecord,
    *,
    state: SandboxCommandJournalState,
    exit_code: int,
) -> SandboxCommandJournalRecord:
    """Helper for control-plane provider tests and lightweight adapters."""

    if not state.terminal:
        raise ValueError("finish 只能写入终态")
    return replace(
        record,
        state=state,
        version=record.version + 1,
        exit_code=exit_code,
    )


__all__ = [
    "RecoverableSandboxCommandCoordinator",
    "RecoveredSandboxCommandExecution",
    "StartedRecoverableSandboxCommand",
    "SandboxCommandAlreadyTerminal",
    "SandboxCommandJournalConflict",
    "SandboxCommandJournalNotFound",
    "SandboxCommandJournalProvider",
    "SandboxCommandJournalRecord",
    "SandboxCommandJournalScope",
    "SandboxCommandJournalState",
    "SandboxCommandJournalUnavailable",
    "SandboxCommandRecoveryInputMismatch",
    "complete_journal_record",
]
