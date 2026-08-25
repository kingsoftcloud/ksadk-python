"""Harness Sandbox Backend（plan §11.3）。

Harness 不绑定 E2B 对象，统一 SandboxBackend 协议：
- ``LocalReadOnlySandboxBackend``：只读本地 Sandbox（最低安全模式），包装
  现有 :class:`HarnessSandboxExecutor`；
- ``GenericSandboxBackend``：接入现有通用 ``ksadk.sandbox`` 后端的适配器位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

    async def execute(
        self, handle: SandboxHandle, request: ExecuteRequest
    ) -> ExecuteResult: ...

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

    async def execute(
        self, handle: SandboxHandle, request: ExecuteRequest
    ) -> ExecuteResult:
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
    "SandboxBackend",
    "SandboxHandle",
    "SandboxPolicyViolation",
    "SandboxSpec",
]
