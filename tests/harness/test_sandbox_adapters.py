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
    SandboxBackendCapabilities,
    SandboxClosedError,
    SandboxPolicyViolation,
    SandboxSpec,
)
from ksadk.harness.sandbox_conformance import (
    SandboxConformanceCase,
    run_sandbox_backend_conformance,
    verify_cooperative_cancellation,
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


def _custom_capabilities(*, artifacts: bool = False) -> SandboxBackendCapabilities:
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


def test_e2b_adapter_kills_background_command_on_task_cancellation():
    vendor = _VendorSandbox()
    backend = adapt_e2b_backend(_FakeE2BBackend(vendor))

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
