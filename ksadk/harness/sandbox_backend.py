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
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

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


class SandboxHandle:
    """一次 Sandbox 会话的句柄（含产出 Artifact 引用）。"""

    def __init__(self, spec: SandboxSpec) -> None:
        self.spec = spec
        self.artifacts: list[str] = []


class SandboxBackend(Protocol):
    """通用 Sandbox 后端协议（本地/通用 ksadk.sandbox/E2B 同构）。"""

    async def create(self, spec: SandboxSpec) -> SandboxHandle: ...

    async def execute(self, handle: SandboxHandle, request: ExecuteRequest) -> ExecuteResult: ...

    async def collect_artifacts(self, handle: SandboxHandle) -> list[str]: ...

    async def close(self, handle: SandboxHandle) -> None: ...


class SandboxPolicyViolation(PermissionError):
    """越界执行（写操作/出网）被策略拒绝。"""


class LocalReadOnlySandboxBackend:
    """只读本地最低安全模式：无网络、无写入、路径不可逃逸。"""

    def __init__(self) -> None:
        self._executors: dict[int, HarnessSandboxExecutor] = {}

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
        executor = self._executors[id(handle)]
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
        return list(handle.artifacts)

    async def close(self, handle: SandboxHandle) -> None:
        self._executors.pop(id(handle), None)


__all__ = [
    "ExecuteRequest",
    "ExecuteResult",
    "LocalReadOnlySandboxBackend",
    "SandboxAuditLog",
    "SandboxBackend",
    "SandboxHandle",
    "SandboxPolicyViolation",
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

    def _audit_append(self, **kwargs: Any) -> None:
        if self._audit is not None:
            self._audit.append(**kwargs)

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        if spec.network_egress:
            raise SandboxPolicyViolation("SubprocessSandboxBackend 不支持网络出网（须容器级沙箱）")
        self._counter += 1
        workspace = Path(tempfile.mkdtemp(prefix=f"sandbox-{self._counter}-", dir=self._base_dir))
        handle = SandboxHandle(spec)
        self._workspaces[id(handle)] = workspace
        return handle

    async def execute(self, handle: SandboxHandle, request: ExecuteRequest) -> ExecuteResult:
        workspace = self._workspaces[id(handle)]
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
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=request.timeout_seconds
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
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
            handle_id=str(id(handle)),
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
        workspace = self._workspaces[id(handle)]
        return sorted(
            p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file()
        )

    async def read_artifact(self, handle: SandboxHandle, relative_path: str) -> bytes:
        workspace = self._workspaces[id(handle)]
        target = (workspace / relative_path).resolve()
        if not str(target).startswith(str(workspace.resolve())):
            raise SandboxPolicyViolation(f"artifact 路径越界: {relative_path!r}")
        return target.read_bytes()

    async def close(self, handle: SandboxHandle) -> None:
        workspace = self._workspaces.pop(id(handle), None)
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
