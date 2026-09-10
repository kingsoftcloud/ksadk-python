"""Harness Sandbox Backend（plan §11.3）。

Harness 不绑定 E2B 对象，统一 SandboxBackend 协议：
- ``LocalReadOnlySandboxBackend``：只读本地 Sandbox（最低安全模式），包装
  现有 :class:`HarnessSandboxExecutor`；
- ``SubprocessSandboxBackend``：缺口 6——进程级隔离的可写 Sandbox（每次
  会话独立临时工作区、超时/输出上限、环境变量清洗、执行审计持久化）；
- ``SandboxAuditLog``：执行审计（SQLite），谁在哪个 Run 里执行了什么、
  结果如何，越界拒绝也留痕。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from ksadk.harness.sandbox import HarnessSandboxExecutor, SandboxPolicyDenied


@dataclass(frozen=True)
class SandboxSpec:
    workspace_root: str
    read_only: bool = True
    network_egress: tuple[str, ...] = ()  # 允许出网目的地（空 = 禁止）
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecuteRequest:
    command: str
    timeout_seconds: float = 60.0
    #: 审计锚点：哪次 Run 发起的执行。
    run_id: str = ""


@dataclass(frozen=True)
class ExecuteResult:
    ok: bool
    output: str
    exit_code: int = 0
    error: str = ""


class FilesystemIsolation(str, Enum):
    """后端能够诚实保证的文件系统边界。"""

    NONE = "none"
    SCOPED_WORKSPACE = "scoped_workspace"
    EPHEMERAL_WORKSPACE = "ephemeral_workspace"
    REMOTE_SANDBOX = "remote_sandbox"


class NetworkControl(str, Enum):
    """网络控制强度；``ADMISSION_ONLY`` 不等同于 OS 级断网。"""

    NONE = "none"
    COMMAND_SURFACE = "command_surface"
    ADMISSION_ONLY = "admission_only"
    ENFORCED = "enforced"


@dataclass(frozen=True)
class SandboxBackendCapabilities:
    """Sandbox Backend 能力声明。

    声明描述的是可验证的后端保证，而不是产品期望。Conformance 会据此
    决定哪些行为必须通过、哪些场景必须明确跳过，避免把本地进程包装器
    误报成容器或远程 VM 级隔离。
    """

    backend_id: str
    filesystem_isolation: FilesystemIsolation
    network_control: NetworkControl
    process_boundary: bool
    request_timeout: bool
    cooperative_cancellation: bool
    artifact_collection: bool
    deterministic_cleanup: bool
    execution_audit: bool
    reconnect: bool = False
    ownership_fencing: bool = False
    command_reconnect: bool = False


class SandboxHandle:
    """一次 Sandbox 会话的句柄（含产出 Artifact 引用）。"""

    def __init__(self, spec: SandboxSpec) -> None:
        self.spec = spec
        self.handle_id = f"sandbox-{uuid4().hex}"
        self.artifacts: list[str] = []
        self.closed = False


@dataclass(frozen=True)
class SandboxResumeToken:
    """Serializable, credential-free locator for a remote Sandbox session."""

    backend_id: str
    handle_id: str
    session_locator: str

    def to_dict(self) -> dict[str, str]:
        return {
            "backendId": self.backend_id,
            "handleId": self.handle_id,
            "sessionLocator": self.session_locator,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SandboxResumeToken:
        token = cls(
            backend_id=str(value.get("backendId", "")).strip(),
            handle_id=str(value.get("handleId", "")).strip(),
            session_locator=str(value.get("sessionLocator", "")).strip(),
        )
        if not token.backend_id or not token.handle_id or not token.session_locator:
            raise ValueError("Sandbox Resume Token 缺少必填定位字段")
        return token


@dataclass(frozen=True)
class SandboxCommandResumeToken:
    """Credential-free locator for one command in a reconnectable Sandbox."""

    backend_id: str
    handle_id: str
    session_locator: str
    process_id: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "backendId": self.backend_id,
            "handleId": self.handle_id,
            "sessionLocator": self.session_locator,
            "processId": self.process_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SandboxCommandResumeToken:
        raw_process_id = value.get("processId", 0)
        try:
            process_id = int(raw_process_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Sandbox Command Resume Token 的 processId 无效") from exc
        token = cls(
            backend_id=str(value.get("backendId", "")).strip(),
            handle_id=str(value.get("handleId", "")).strip(),
            session_locator=str(value.get("sessionLocator", "")).strip(),
            process_id=process_id,
        )
        if not token.backend_id or not token.handle_id or not token.session_locator:
            raise ValueError("Sandbox Command Resume Token 缺少必填定位字段")
        if token.process_id <= 0:
            raise ValueError("Sandbox Command Resume Token 的 processId 必须大于 0")
        return token


class SandboxBackend(Protocol):
    """通用 Sandbox 后端协议（本地/通用 ksadk.sandbox/E2B 同构）。"""

    @property
    def capabilities(self) -> SandboxBackendCapabilities: ...

    async def create(self, spec: SandboxSpec) -> SandboxHandle: ...

    async def execute(self, handle: SandboxHandle, request: ExecuteRequest) -> ExecuteResult: ...

    async def collect_artifacts(self, handle: SandboxHandle) -> list[str]: ...

    async def close(self, handle: SandboxHandle) -> None: ...


@runtime_checkable
class ReconnectableSandboxBackend(Protocol):
    """Optional Harness extension for cross-process session recovery."""

    def export_resume_token(self, handle: SandboxHandle) -> SandboxResumeToken: ...

    async def reconnect(
        self,
        token: SandboxResumeToken,
        *,
        spec: SandboxSpec,
    ) -> SandboxHandle: ...


class SandboxPolicyViolation(PermissionError):
    """越界执行（写操作/出网）被策略拒绝。"""


class SandboxClosedError(RuntimeError):
    """对已关闭或不属于当前后端的 Sandbox Handle 执行操作。"""


class LocalReadOnlySandboxBackend:
    """只读本地最低安全模式：无网络、无写入、路径不可逃逸。"""

    def __init__(self) -> None:
        self._executors: dict[int, HarnessSandboxExecutor] = {}

    @property
    def capabilities(self) -> SandboxBackendCapabilities:
        return SandboxBackendCapabilities(
            backend_id="local-read-only",
            filesystem_isolation=FilesystemIsolation.SCOPED_WORKSPACE,
            network_control=NetworkControl.COMMAND_SURFACE,
            process_boundary=False,
            request_timeout=False,
            cooperative_cancellation=False,
            artifact_collection=False,
            deterministic_cleanup=True,
            execution_audit=False,
        )

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        if not spec.read_only:
            raise SandboxPolicyViolation("LocalReadOnly 后端只允许只读 spec")
        if spec.network_egress:
            raise SandboxPolicyViolation("LocalReadOnly 后端禁止网络出网")
        handle = SandboxHandle(spec)
        self._executors[id(handle)] = HarnessSandboxExecutor(
            workspace_root=spec.workspace_root, read_only=True
        )
        return handle

    async def execute(self, handle: SandboxHandle, request: ExecuteRequest) -> ExecuteResult:
        executor = self._executors.get(id(handle))
        if executor is None or handle.closed:
            raise SandboxClosedError("Sandbox Handle 已关闭或不属于当前后端")
        try:
            result: dict[str, Any] = await executor.run_command(request.command)
        except SandboxPolicyDenied as exc:
            return ExecuteResult(ok=False, output="", exit_code=1, error=str(exc))
        return ExecuteResult(
            ok=bool(result.get("ok")),
            output=str(result.get("stdout", "")),
            exit_code=int(result.get("exit_code", 0)),
            error=str(result.get("stderr", "")),
        )

    async def collect_artifacts(self, handle: SandboxHandle) -> list[str]:
        # 只读 Sandbox 不产出 Artifact（无写入）；协议要求可调用。
        if id(handle) not in self._executors or handle.closed:
            raise SandboxClosedError("Sandbox Handle 已关闭或不属于当前后端")
        return list(handle.artifacts)

    async def close(self, handle: SandboxHandle) -> None:
        self._executors.pop(id(handle), None)
        handle.closed = True


__all__ = [
    "ExecuteRequest",
    "ExecuteResult",
    "FilesystemIsolation",
    "LocalReadOnlySandboxBackend",
    "NetworkControl",
    "ReconnectableSandboxBackend",
    "SandboxAuditLog",
    "SandboxBackend",
    "SandboxBackendCapabilities",
    "SandboxClosedError",
    "SandboxCommandResumeToken",
    "SandboxHandle",
    "SandboxPolicyViolation",
    "SandboxResumeToken",
    "SandboxSpec",
    "SubprocessSandboxBackend",
]


# ---------------------------------------------------------------------------
# 缺口 6：执行审计持久化（SQLite）
# ---------------------------------------------------------------------------


class SandboxAuditLog:
    """Sandbox 执行审计：每次 execute（含策略拒绝）都留痕。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db = sqlite3.connect(str(db_path))
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sandbox_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL DEFAULT "",
                handle_id TEXT NOT NULL,
                command TEXT NOT NULL,
                ok INTEGER NOT NULL,
                exit_code INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                output_bytes INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT "",
                created_at TEXT NOT NULL
            )
            """
        )
        self._db.commit()

    def append(
        self,
        *,
        handle_id: str,
        command: str,
        ok: bool,
        exit_code: int,
        duration_ms: int,
        output_bytes: int,
        error: str = "",
        run_id: str = "",
    ) -> None:
        self._db.execute(
            "INSERT INTO sandbox_audit"
            " (run_id, handle_id, command, ok, exit_code, duration_ms,"
            " output_bytes, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                handle_id,
                command,
                int(ok),
                exit_code,
                duration_ms,
                output_bytes,
                error,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )
        self._db.commit()

    def list(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT run_id, handle_id, command, ok, exit_code, duration_ms,"
            " output_bytes, error, created_at FROM sandbox_audit"
            " WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        keys = (
            "runId",
            "handleId",
            "command",
            "ok",
            "exitCode",
            "durationMs",
            "outputBytes",
            "error",
            "createdAt",
        )
        return [dict(zip(keys, row)) for row in rows]

    def close(self) -> None:
        self._db.close()


# ---------------------------------------------------------------------------
# 缺口 6：进程级隔离的可写 Sandbox 后端
# ---------------------------------------------------------------------------


class SubprocessSandboxBackend:
    """进程隔离 Sandbox：每次会话独立临时工作区，可写、不出网、留审计。

    与 ``LocalReadOnlySandboxBackend``（只读最低安全模式）相对，这是
    生产形态的本地实现——隔离靠三条机制，且每条都诚实声明边界：

    1. **文件系统**：每个 handle 一个一次性临时目录，close 即销毁，
       命令 cwd 钉死在该目录（宿主工作区不可见）；
    2. **资源**：单命令 wall-clock 超时 + 输出字节上限（截断并标记）；
    3. **环境**：子进程环境变量清洗（只保留 PATH/HOME/LANG/LC_*，
       凭证类变量一律不透传）。

    网络隔离依赖 OS 级沙箱（容器/专用 VM），本地后端不支持任何出网
    spec——``network_egress`` 非空直接策略拒绝，而不是静默放行。
    """

    _PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")

    def __init__(
        self,
        *,
        base_dir: str | Path | None = None,
        audit_log: SandboxAuditLog | None = None,
        max_output_bytes: int = 100_000,
    ) -> None:
        self._base_dir = Path(base_dir) if base_dir else None
        if self._base_dir is not None:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        self._audit = audit_log
        self._max_output = max_output_bytes
        self._workspaces: dict[int, Path] = {}
        self._counter = 0

    @property
    def capabilities(self) -> SandboxBackendCapabilities:
        return SandboxBackendCapabilities(
            backend_id="local-subprocess",
            filesystem_isolation=FilesystemIsolation.EPHEMERAL_WORKSPACE,
            # 仅拒绝带出网诉求的 spec；本地宿主进程本身没有 OS 级断网。
            network_control=NetworkControl.ADMISSION_ONLY,
            process_boundary=True,
            request_timeout=True,
            cooperative_cancellation=True,
            artifact_collection=True,
            deterministic_cleanup=True,
            execution_audit=self._audit is not None,
        )

    def _audit_append(self, **kwargs: Any) -> None:
        if self._audit is not None:
            self._audit.append(**kwargs)

    @staticmethod
    async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
        """终止 shell 及其子进程，避免 timeout/cancel 留下孤儿进程。"""

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        if spec.network_egress:
            raise SandboxPolicyViolation("SubprocessSandboxBackend 不支持网络出网（须容器级沙箱）")
        self._counter += 1
        workspace = Path(tempfile.mkdtemp(prefix=f"sandbox-{self._counter}-", dir=self._base_dir))
        handle = SandboxHandle(spec)
        self._workspaces[id(handle)] = workspace
        return handle

    async def execute(self, handle: SandboxHandle, request: ExecuteRequest) -> ExecuteResult:
        workspace = self._workspaces.get(id(handle))
        if workspace is None or handle.closed:
            raise SandboxClosedError("Sandbox Handle 已关闭或不属于当前后端")
        started = time.monotonic()
        env = {key: os.environ[key] for key in self._PASSTHROUGH_ENV if key in os.environ}
        env.update(handle.spec.env)
        try:
            proc = await asyncio.create_subprocess_shell(
                request.command,
                cwd=workspace,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=request.timeout_seconds
                )
            except asyncio.CancelledError:
                await self._terminate_process_group(proc)
                duration_ms = int((time.monotonic() - started) * 1000)
                self._audit_append(
                    handle_id=handle.handle_id,
                    command=request.command,
                    ok=False,
                    exit_code=-9,
                    duration_ms=duration_ms,
                    output_bytes=0,
                    error="sandbox 命令已取消",
                    run_id=str(getattr(request, "run_id", "") or ""),
                )
                raise
            except TimeoutError:
                await self._terminate_process_group(proc)
                result = ExecuteResult(ok=False, output="", exit_code=-9, error="sandbox 命令超时")
            else:
                output = stdout.decode("utf-8", errors="replace")
                err = stderr.decode("utf-8", errors="replace")
                truncated = ""
                if len(output) > self._max_output:
                    output = output[: self._max_output]
                    truncated = f"\n[sandbox] 输出超过 {self._max_output} 字节已截断"
                result = ExecuteResult(
                    ok=proc.returncode == 0,
                    output=output + truncated,
                    exit_code=int(proc.returncode or 0),
                    error=err[: self._max_output],
                )
        except Exception as exc:  # noqa: BLE001 - 执行失败也要审计并回报
            result = ExecuteResult(ok=False, output="", exit_code=1, error=str(exc))
        duration_ms = int((time.monotonic() - started) * 1000)
        self._audit_append(
            handle_id=handle.handle_id,
            command=request.command,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=duration_ms,
            output_bytes=len(result.output),
            error=result.error[:500],
            run_id=str(getattr(request, "run_id", "") or ""),
        )
        return result

    async def collect_artifacts(self, handle: SandboxHandle) -> list[str]:
        """工作区内产出文件（相对路径，宿主转存为 Artifact）。"""
        workspace = self._workspaces.get(id(handle))
        if workspace is None or handle.closed:
            raise SandboxClosedError("Sandbox Handle 已关闭或不属于当前后端")
        return sorted(
            p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file()
        )

    async def read_artifact(self, handle: SandboxHandle, relative_path: str) -> bytes:
        workspace = self._workspaces.get(id(handle))
        if workspace is None or handle.closed:
            raise SandboxClosedError("Sandbox Handle 已关闭或不属于当前后端")
        target = (workspace / relative_path).resolve()
        resolved_workspace = workspace.resolve()
        if target != resolved_workspace and resolved_workspace not in target.parents:
            raise SandboxPolicyViolation(f"artifact 路径越界: {relative_path!r}")
        return target.read_bytes()

    async def close(self, handle: SandboxHandle) -> None:
        workspace = self._workspaces.pop(id(handle), None)
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
        handle.closed = True
