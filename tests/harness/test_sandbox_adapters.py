from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass

import pytest

from ksadk.harness.sandbox_adapters import (
    SessionSandboxBackendAdapter,
    adapt_e2b_backend,
    adapt_local_process_backend,
    adapt_sdk_sandbox_backend,
)
from ksadk.harness.sandbox_backend import (
    ExecuteRequest,
    FilesystemIsolation,
    NetworkControl,
    SandboxAuditLog,
    SandboxBackendCapabilities,
    SandboxClosedError,
    SandboxPolicyViolation,
    SandboxResumeToken,
    SandboxSpec,
)
from ksadk.harness.sandbox_conformance import (
    SandboxConformanceCase,
    run_sandbox_backend_conformance,
    verify_cooperative_cancellation,
)
from ksadk.harness.sandbox_lease import (
    SandboxLeaseConflict,
    SandboxLeaseGrant,
)
from ksadk.sandbox.backends.e2b import E2BSandboxBackend, E2BSandboxSession
from ksadk.sandbox.backends.local_process import LocalProcessSandboxBackend
from ksadk.sandbox.base import SandboxCommandResult
from ksadk.sandbox.base import SandboxSpec as SdkSandboxSpec


def _run(coro):
    return asyncio.run(coro)


class FakeSession:
    sandbox_id = "fake-session"

    def __init__(self) -> None:
        self.killed = False
        self.last_timeout: int | None = None
        self.last_env: dict[str, str] = {}

    def run_command(self, command, *, timeout=None, env=None, cwd=None):
        self.last_timeout = timeout
        self.last_env = dict(env or {})
        if command == "timeout":
            return SandboxCommandResult(stderr="command timed out", exit_code=124)
        return SandboxCommandResult(stdout=f"ran:{command}", exit_code=0)

    def write_file(self, path, data):
        return None

    def read_file(self, path):
        return ""

    def get_host(self, port):
        return f"fake:{port}"

    def kill(self):
        self.killed = True


class FakeBackend:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.created_session_id = ""

    def create_session(self, *, session_id, env=None, input_files=None):
        self.created_session_id = session_id
        return self.session


def _custom_capabilities(
    *,
    artifacts: bool = False,
    reconnect: bool = False,
) -> SandboxBackendCapabilities:
    return SandboxBackendCapabilities(
        backend_id="fake-sdk",
        filesystem_isolation=FilesystemIsolation.REMOTE_SANDBOX,
        network_control=NetworkControl.ADMISSION_ONLY,
        process_boundary=True,
        request_timeout=True,
        cooperative_cancellation=False,
        artifact_collection=artifacts,
        deterministic_cleanup=True,
        execution_audit=False,
        reconnect=reconnect,
    )


def test_sdk_adapter_translates_session_contract_and_closes_handle():
    sdk = FakeBackend()
    backend = SessionSandboxBackendAdapter(
        sdk,
        capabilities=_custom_capabilities(),
    )

    async def flow():
        handle = await backend.create(
            SandboxSpec(workspace_root="", read_only=False, env={"KSADK_SAFE_MODE": "1"})
        )
        result = await backend.execute(
            handle,
            ExecuteRequest(command="printf ok", timeout_seconds=1.2),
        )
        await backend.close(handle)
        with pytest.raises(SandboxClosedError):
            await backend.execute(handle, ExecuteRequest(command="printf late"))
        return handle, result

    handle, result = _run(flow())
    assert result.ok is True
    assert result.output == "ran:printf ok"
    assert sdk.created_session_id == handle.handle_id
    assert sdk.session.last_timeout == 2
    assert sdk.session.last_env == {"KSADK_SAFE_MODE": "1"}
    assert sdk.session.killed is True
    assert handle.closed is True


def test_sdk_adapter_normalizes_timeout_and_artifact_collector():
    sdk = FakeBackend()
    backend = SessionSandboxBackendAdapter(
        sdk,
        capabilities=_custom_capabilities(artifacts=True),
        artifact_collector=lambda session: ["z.txt", "a.json"],
    )

    async def flow():
        handle = await backend.create(SandboxSpec(workspace_root="", read_only=False))
        result = await backend.execute(handle, ExecuteRequest(command="timeout"))
        artifacts = await backend.collect_artifacts(handle)
        await backend.close(handle)
        return result, artifacts

    result, artifacts = _run(flow())
    assert result.ok is False
    assert result.exit_code == 124
    assert "超时" in result.error
    assert artifacts == ["a.json", "z.txt"]


def test_sdk_adapter_rejects_unenforceable_read_only_and_domain_allowlist():
    backend = SessionSandboxBackendAdapter(
        FakeBackend(),
        capabilities=_custom_capabilities(),
    )

    with pytest.raises(SandboxPolicyViolation, match="只读"):
        _run(backend.create(SandboxSpec(workspace_root="", read_only=True)))
    with pytest.raises(SandboxPolicyViolation, match="allowlist"):
        _run(
            backend.create(
                SandboxSpec(
                    workspace_root="",
                    read_only=False,
                    network_egress=("example.invalid",),
                )
            )
        )


def test_sdk_adapter_rejects_false_reconnect_capability_claim():
    with pytest.raises(ValueError, match="reconnect_session"):
        SessionSandboxBackendAdapter(
            FakeBackend(),
            capabilities=_custom_capabilities(reconnect=True),
        )


def test_local_process_adapter_passes_declared_conformance(tmp_path):
    sdk = LocalProcessSandboxBackend(workspace_root=tmp_path, backend_name="local_process")
    backend = adapt_local_process_backend(sdk)

    report = _run(
        run_sandbox_backend_conformance(
            backend,
            spec=SandboxSpec(workspace_root=str(tmp_path), read_only=False),
            case=SandboxConformanceCase(
                smoke_command="printf adapter-ok",
                expected_output="adapter-ok",
                timeout_command="sleep 1",
            ),
        )
    )

    assert report.passed, report.findings
    assert backend.capabilities.filesystem_isolation is FilesystemIsolation.SCOPED_WORKSPACE
    assert backend.capabilities.network_control is NetworkControl.ADMISSION_ONLY
    assert backend.capabilities.deterministic_cleanup is False


def test_local_process_adapter_rejects_workspace_mismatch(tmp_path):
    backend = adapt_sdk_sandbox_backend(
        LocalProcessSandboxBackend(workspace_root=tmp_path / "configured")
    )

    with pytest.raises(SandboxPolicyViolation, match="workspace_root"):
        _run(backend.create(SandboxSpec(workspace_root=str(tmp_path / "other"), read_only=False)))


def test_e2b_profile_does_not_overclaim_network_allowlist():
    denied = adapt_e2b_backend(
        E2BSandboxBackend(
            spec=SdkSandboxSpec(template_id="fake", allow_internet_access=False),
            sandbox_cls=object,
        )
    )
    unrestricted = adapt_e2b_backend(
        E2BSandboxBackend(
            spec=SdkSandboxSpec(template_id="fake", allow_internet_access=True),
            sandbox_cls=object,
        )
    )

    assert denied.capabilities.network_control is NetworkControl.ENFORCED
    assert unrestricted.capabilities.network_control is NetworkControl.NONE
    assert denied.capabilities.cooperative_cancellation is True
    assert denied.capabilities.artifact_collection is True


@dataclass
class _VendorResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class _VendorCommandHandle:
    def __init__(self, result: _VendorResult, *, blocks: bool = False) -> None:
        self._result = result
        self._blocks = blocks
        self._killed = threading.Event()

    def wait(self):
        if self._blocks:
            self._killed.wait(timeout=2)
        return self._result

    def kill(self):
        self._killed.set()
        return True


class _VendorCommands:
    def __init__(self) -> None:
        self.handles: list[_VendorCommandHandle] = []

    def run(self, command, **kwargs):
        if not kwargs.get("background"):
            return _VendorResult()
        handle = _VendorCommandHandle(
            _VendorResult(stdout=f"ran:{command}"),
            blocks=command == "sleep forever",
        )
        self.handles.append(handle)
        return handle


class _EntryType:
    value = "file"


@dataclass
class _VendorEntry:
    path: str
    type: object = _EntryType()
    symlink_target: str | None = None


class _VendorFiles:
    def write(self, path, data):
        return None

    def write_files(self, files):
        return None

    def read(self, path):
        return ""

    def list(self, path, *, depth=None):
        return [
            _VendorEntry(f"{path}/report.json"),
            _VendorEntry(f"{path}/nested/evidence.txt"),
            _VendorEntry("relative.txt"),
            _VendorEntry(f"{path}/outside-link", symlink_target="/tmp/outside.txt"),
            _VendorEntry("/tmp/outside.txt"),
        ]


class _VendorSandbox:
    sandbox_id = "vendor-e2b"

    def __init__(self) -> None:
        self.commands = _VendorCommands()
        self.files = _VendorFiles()
        self.killed = False

    def kill(self):
        self.killed = True


class _FakeE2BBackend(E2BSandboxBackend):
    def __init__(self, vendor: _VendorSandbox):
        super().__init__(
            spec=SdkSandboxSpec(
                template_id="fake",
                allow_internet_access=False,
            ),
            sandbox_cls=object,
        )
        self.vendor = vendor

    def create_session(self, *, session_id, env=None, input_files=None):
        return E2BSandboxSession(self.vendor)

    def reconnect_session(self, *, session_locator):
        if session_locator != self.vendor.sandbox_id:
            raise RuntimeError("unknown sandbox")
        return E2BSandboxSession(self.vendor)


class _FakeLeaseProvider:
    def __init__(self) -> None:
        self.now = 100.0
        self._grants: dict[str, SandboxLeaseGrant] = {}
        self._last_fencing: dict[str, int] = {}

    async def acquire(
        self,
        *,
        backend_id,
        handle_id,
        owner_id,
        ttl_seconds,
    ):
        current = self._grants.get(handle_id)
        if current is not None and current.expires_at > self.now:
            raise SandboxLeaseConflict("sandbox lease already owned")
        fencing = self._last_fencing.get(handle_id, 0) + 1
        grant = SandboxLeaseGrant(
            backend_id=backend_id,
            handle_id=handle_id,
            owner_id=owner_id,
            fencing_token=fencing,
            expires_at=self.now + ttl_seconds,
        )
        self._last_fencing[handle_id] = fencing
        self._grants[handle_id] = grant
        return grant

    async def renew(self, grant, *, ttl_seconds):
        current = self._grants.get(grant.handle_id)
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
            owner_id=grant.owner_id,
            fencing_token=grant.fencing_token,
            expires_at=self.now + ttl_seconds,
        )
        self._grants[grant.handle_id] = renewed
        return renewed

    async def release(self, grant):
        current = self._grants.get(grant.handle_id)
        if (
            current is None
            or current.owner_id != grant.owner_id
            or current.fencing_token != grant.fencing_token
        ):
            raise SandboxLeaseConflict("stale sandbox fencing token")
        self._grants.pop(grant.handle_id)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_e2b_adapter_collects_only_workspace_artifacts_and_runs_in_workspace():
    vendor = _VendorSandbox()
    backend = adapt_e2b_backend(_FakeE2BBackend(vendor))

    async def flow():
        handle = await backend.create(
            SandboxSpec(workspace_root="/tmp/ksadk-artifacts", read_only=False)
        )
        result = await backend.execute(handle, ExecuteRequest(command="produce report"))
        artifacts = await backend.collect_artifacts(handle)
        await backend.close(handle)
        return result, artifacts

    result, artifacts = _run(flow())
    assert result.ok is True
    assert result.output == "ran:produce report"
    assert artifacts == ["nested/evidence.txt", "relative.txt", "report.json"]
    assert vendor.killed is True


def test_e2b_adapter_persists_execution_audit(tmp_path):
    vendor = _VendorSandbox()
    audit = SandboxAuditLog(tmp_path / "e2b-audit.sqlite3")
    backend = adapt_e2b_backend(_FakeE2BBackend(vendor), audit_log=audit)

    async def flow():
        handle = await backend.create(
            SandboxSpec(workspace_root="/tmp/ksadk-audit", read_only=False)
        )
        result = await backend.execute(
            handle,
            ExecuteRequest(command="produce report", run_id="run-e2b-audit"),
        )
        await backend.close(handle)
        return handle, result

    handle, result = _run(flow())
    rows = audit.list("run-e2b-audit")
    audit.close()

    assert result.ok is True
    assert backend.capabilities.execution_audit is True
    assert rows == [
        {
            "runId": "run-e2b-audit",
            "handleId": handle.handle_id,
            "command": "produce report",
            "ok": 1,
            "exitCode": 0,
            "durationMs": rows[0]["durationMs"],
            "outputBytes": len("ran:produce report".encode()),
            "error": "",
            "createdAt": rows[0]["createdAt"],
        }
    ]


def test_e2b_adapter_reconnects_from_serializable_token_across_instances():
    vendor = _VendorSandbox()
    sdk = _FakeE2BBackend(vendor)
    first_backend = adapt_e2b_backend(sdk)
    spec = SandboxSpec(
        workspace_root="/tmp/ksadk-reconnect",
        read_only=False,
        env={"RUNTIME_VALUE": "resolved-again"},
    )

    async def create_and_export():
        handle = await first_backend.create(spec)
        return handle, first_backend.export_resume_token(handle).to_dict()

    original, payload = _run(create_and_export())
    token = SandboxResumeToken.from_dict(payload)
    second_backend = adapt_e2b_backend(sdk)

    async def reconnect_and_execute():
        handle = await second_backend.reconnect(token, spec=spec)
        result = await second_backend.execute(
            handle,
            ExecuteRequest(command="resume work", run_id="run-reconnected"),
        )
        await second_backend.close(handle)
        return handle, result

    resumed, result = _run(reconnect_and_execute())

    assert second_backend.capabilities.reconnect is True
    assert second_backend.capabilities.ownership_fencing is False
    assert payload == {
        "backendId": "sdk-e2b",
        "handleId": original.handle_id,
        "sessionLocator": "vendor-e2b",
    }
    assert "resolved-again" not in str(payload)
    assert resumed.handle_id == original.handle_id
    assert resumed.session.sandbox_id == "vendor-e2b"
    assert result.ok is True
    assert result.output == "ran:resume work"
    assert vendor.commands.handles[-1]._result.stdout == "ran:resume work"


def test_e2b_adapter_fences_stale_owner_after_cross_process_takeover():
    vendor = _VendorSandbox()
    sdk = _FakeE2BBackend(vendor)
    leases = _FakeLeaseProvider()
    spec = SandboxSpec(workspace_root="/tmp/ksadk-fencing", read_only=False)
    first_backend = adapt_e2b_backend(
        sdk,
        lease_provider=leases,
        lease_owner_id="process-a",
        lease_ttl_seconds=5,
    )

    async def flow():
        original = await first_backend.create(spec)
        token = SandboxResumeToken.from_dict(first_backend.export_resume_token(original).to_dict())
        assert original.lease is not None
        assert original.lease.fencing_token == 1

        leases.advance(6)
        second_backend = adapt_e2b_backend(
            sdk,
            lease_provider=leases,
            lease_owner_id="process-b",
            lease_ttl_seconds=5,
        )
        resumed = await second_backend.reconnect(token, spec=spec)
        assert resumed.lease is not None
        assert resumed.lease.fencing_token == 2

        with pytest.raises(SandboxLeaseConflict, match="stale"):
            await first_backend.execute(original, ExecuteRequest(command="stale-write"))
        with pytest.raises(SandboxLeaseConflict, match="stale"):
            await first_backend.close(original)
        assert vendor.killed is False

        result = await second_backend.execute(
            resumed,
            ExecuteRequest(command="owner-b-write"),
        )
        await second_backend.close(resumed)
        return second_backend, original, resumed, result

    second_backend, original, resumed, result = _run(flow())
    assert second_backend.capabilities.ownership_fencing is True
    assert original.closed is True
    assert resumed.closed is True
    assert result.ok is True
    assert result.output == "ran:owner-b-write"
    assert vendor.killed is True


def test_session_adapter_rejects_fencing_declaration_without_provider():
    vendor = _VendorSandbox()

    with pytest.raises(ValueError, match="lease_provider"):
        SessionSandboxBackendAdapter(
            _FakeE2BBackend(vendor),
            capabilities=SandboxBackendCapabilities(
                backend_id="custom-e2b",
                filesystem_isolation=FilesystemIsolation.REMOTE_SANDBOX,
                network_control=NetworkControl.ENFORCED,
                process_boundary=True,
                request_timeout=True,
                cooperative_cancellation=False,
                artifact_collection=False,
                deterministic_cleanup=True,
                execution_audit=False,
                reconnect=True,
                ownership_fencing=True,
            ),
        )


def test_e2b_adapter_rejects_empty_lease_owner_id():
    with pytest.raises(ValueError, match="lease_owner_id"):
        adapt_e2b_backend(
            _FakeE2BBackend(_VendorSandbox()),
            lease_provider=_FakeLeaseProvider(),
            lease_owner_id="   ",
        )


def test_sandbox_resume_token_rejects_missing_locator():
    with pytest.raises(ValueError, match="缺少"):
        SandboxResumeToken.from_dict(
            {
                "backendId": "sdk-e2b",
                "handleId": "sandbox-1",
                "sessionLocator": "",
            }
        )


def test_e2b_adapter_kills_background_command_and_audits_cancellation(tmp_path):
    vendor = _VendorSandbox()
    audit = SandboxAuditLog(tmp_path / "e2b-cancel-audit.sqlite3")
    backend = adapt_e2b_backend(_FakeE2BBackend(vendor), audit_log=audit)

    report = _run(
        verify_cooperative_cancellation(
            backend,
            spec=SandboxSpec(
                workspace_root="/tmp/ksadk-cancel",
                read_only=False,
            ),
            command="sleep forever",
        )
    )

    assert report.passed, report.findings
    assert [item.status for item in report.findings] == ["passed"]
    assert vendor.commands.handles[-1]._killed.is_set()
    rows = audit.list("sandbox-conformance-cancel")
    audit.close()
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert rows[0]["exitCode"] == 130
    assert rows[0]["error"] == "sandbox 命令已取消"


def test_custom_sdk_backend_requires_explicit_capabilities():
    with pytest.raises(ValueError, match="capabilities"):
        adapt_sdk_sandbox_backend(FakeBackend())


def test_real_e2b_backend_conformance_when_explicitly_enabled():
    if os.environ.get("KSADK_REAL_SANDBOX_E2E") != "1":
        pytest.skip("set KSADK_REAL_SANDBOX_E2E=1 to run remote sandbox conformance")
    template_id = os.environ.get("KSADK_SANDBOX_TEMPLATE_ID", "").strip()
    if not template_id:
        pytest.skip("KSADK_SANDBOX_TEMPLATE_ID is required for remote conformance")

    sdk = E2BSandboxBackend(
        spec=SdkSandboxSpec(
            template_id=template_id,
            timeout=120,
            allow_internet_access=False,
        )
    )
    backend = adapt_e2b_backend(sdk)
    report = _run(
        run_sandbox_backend_conformance(
            backend,
            spec=SandboxSpec(
                workspace_root="/tmp/ksadk-harness-conformance",
                read_only=False,
            ),
            case=SandboxConformanceCase(
                smoke_command="printf remote-conformance-ok",
                expected_output="remote-conformance-ok",
                timeout_command="sleep 5",
                artifact_command="printf artifact-ok > report.txt",
                expected_artifact="report.txt",
            ),
        )
    )
    assert report.passed, report.findings

    cancel_report = _run(
        verify_cooperative_cancellation(
            backend,
            spec=SandboxSpec(
                workspace_root="/tmp/ksadk-harness-cancel",
                read_only=False,
            ),
            command="sleep 30",
        )
    )
    assert cancel_report.passed, cancel_report.findings

    reconnect_source = adapt_e2b_backend(sdk)
    reconnect_spec = SandboxSpec(
        workspace_root="/tmp/ksadk-harness-reconnect",
        read_only=False,
    )

    async def reconnect_across_adapters():
        original = await reconnect_source.create(reconnect_spec)
        token = SandboxResumeToken.from_dict(
            reconnect_source.export_resume_token(original).to_dict()
        )
        resumed_backend = adapt_e2b_backend(sdk)
        resumed = await resumed_backend.reconnect(token, spec=reconnect_spec)
        result = await resumed_backend.execute(
            resumed,
            ExecuteRequest(command="printf remote-reconnect-ok"),
        )
        await resumed_backend.close(resumed)
        return original, resumed, result

    original, resumed, result = _run(reconnect_across_adapters())
    assert resumed.handle_id == original.handle_id
    assert result.ok is True
    assert result.output == "remote-reconnect-ok"
