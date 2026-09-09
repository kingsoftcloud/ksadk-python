from __future__ import annotations

import logging
import os
import posixpath
import time
from typing import Any

from ksadk.sandbox.base import (
    ReconnectableSandboxCommandHandle,
    SandboxCommandResult,
    SandboxError,
    SandboxInputFile,
    SandboxSession,
    SandboxSpec,
)
from ksadk.sandbox.e2b_connection import ExplicitE2BConnection

logger = logging.getLogger(__name__)

_TRANSIENT_STARTUP_ERROR_NAMES = {
    "NotFoundException",
    "FileNotFoundException",
    "SandboxNotFoundException",
    # create 快速返回后 envd RPC 通道可能尚未就绪 (connection refused / unavailable),
    # SDK 将其包装为 TimeoutException 抛出, 需按瞬时启动错误重试。
    "TimeoutException",
}


def _startup_retry_attempts() -> int:
    raw = os.environ.get("KSADK_SANDBOX_STARTUP_RETRY_ATTEMPTS", "6")
    try:
        return max(1, int(raw))
    except ValueError:
        return 6


def _startup_retry_delay() -> float:
    raw = os.environ.get("KSADK_SANDBOX_STARTUP_RETRY_DELAY", "0.2")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.2


def _is_transient_startup_error(exc: Exception) -> bool:
    return type(exc).__name__ in _TRANSIENT_STARTUP_ERROR_NAMES


def _with_startup_retry(operation):
    attempts = _startup_retry_attempts()
    delay = _startup_retry_delay()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if not _is_transient_startup_error(exc) or attempt >= attempts - 1:
                raise
            last_exc = exc
            time.sleep(min(delay * (2**attempt), 1.0))
    if last_exc is not None:
        raise last_exc
    raise SandboxError("E2B sandbox startup retry failed unexpectedly")


class E2BCommandHandle:
    """Normalize an E2B background command to the SDK optional contract."""

    def __init__(self, handle: Any):
        self._handle = handle

    @property
    def process_id(self) -> int:
        process_id = int(getattr(self._handle, "pid", 0) or 0)
        if process_id <= 0:
            raise SandboxError("E2B command handle did not expose a valid process ID")
        return process_id

    def wait(self) -> SandboxCommandResult:
        try:
            result = self._handle.wait()
        except Exception as exc:
            # E2B raises CommandExitException for an ordinary non-zero exit.
            # Preserve the result contract instead of erasing stdout/stderr.
            exit_code = getattr(exc, "exit_code", None)
            if exit_code is None:
                raise
            return SandboxCommandResult(
                stdout=str(getattr(exc, "stdout", "") or ""),
                stderr=str(getattr(exc, "stderr", "") or ""),
                exit_code=int(exit_code),
            )
        return SandboxCommandResult(
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
            exit_code=getattr(result, "exit_code", None),
        )

    def kill(self) -> bool:
        return bool(self._handle.kill())


class E2BSandboxSession:
    def __init__(self, sandbox: Any):
        self._sandbox = sandbox

    @property
    def sandbox_id(self) -> str:
        return str(getattr(self._sandbox, "sandbox_id", "") or "")

    def write_file(self, path: str, data: str | bytes) -> None:
        self._sandbox.files.write(path, data)

    def write_files(self, files: list[tuple[str, str | bytes]]) -> None:
        if not files:
            return
        self._sandbox.files.write_files([{"path": path, "data": data} for path, data in files])

    def read_file(self, path: str) -> str:
        return str(self._sandbox.files.read(path))

    def read_file_bytes(self, path: str, *, max_bytes: int) -> bytes:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("File download limit must be positive")
        stream = self._sandbox.files.read(path, format="stream", request_timeout=30)
        content = bytearray()
        try:
            for chunk in stream:
                if len(content) + len(chunk) > max_bytes:
                    raise ValueError("Sandbox file exceeds download limit")
                content.extend(chunk)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return bytes(content)

    def run_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> SandboxCommandResult:
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if env is not None:
            kwargs["envs"] = env
        if cwd is not None:
            kwargs["cwd"] = cwd
        result = self._sandbox.commands.run(command, **kwargs)
        return SandboxCommandResult(
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
            exit_code=getattr(result, "exit_code", None),
        )

    def start_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> ReconnectableSandboxCommandHandle:
        kwargs: dict[str, Any] = {"background": True}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if env is not None:
            kwargs["envs"] = env
        if cwd is not None:
            kwargs["cwd"] = cwd
        return E2BCommandHandle(self._sandbox.commands.run(command, **kwargs))

    def connect_command(
        self,
        process_id: int,
        *,
        timeout: int | None = None,
    ) -> ReconnectableSandboxCommandHandle:
        if process_id <= 0:
            raise SandboxError("E2B command reconnect requires a positive process ID")
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return E2BCommandHandle(self._sandbox.commands.connect(process_id, **kwargs))

    def list_files(self, root: str, *, recursive: bool = True) -> list[str]:
        """List file paths below ``root`` without exposing sibling paths."""

        normalized_root = posixpath.normpath(root)
        if not normalized_root.startswith("/"):
            raise SandboxError("E2B artifact root must be an absolute path")
        entries = self._sandbox.files.list(
            normalized_root,
            depth=None if recursive else 1,
        )
        files: list[str] = []
        for entry in entries:
            entry_path = posixpath.normpath(str(getattr(entry, "path", "") or ""))
            if not entry_path.startswith("/"):
                entry_path = posixpath.normpath(posixpath.join(normalized_root, entry_path))
            entry_type = getattr(getattr(entry, "type", None), "value", "")
            if getattr(entry, "symlink_target", None) is not None:
                continue
            try:
                in_root = posixpath.commonpath((normalized_root, entry_path)) == normalized_root
            except ValueError:
                in_root = False
            if not in_root or entry_type != "file":
                continue
            relative = posixpath.relpath(entry_path, normalized_root)
            if relative != "." and not relative.startswith("../"):
                files.append(relative)
        return sorted(set(files))

    def get_host(self, port: int) -> str:
        return str(self._sandbox.get_host(port))

    def kill(self) -> None:
        self._sandbox.kill()


class E2BSandboxBackend:
    def __init__(
        self,
        *,
        spec: SandboxSpec,
        sandbox_cls: Any | None = None,
        connection: ExplicitE2BConnection | None = None,
    ):
        if not spec.template_id:
            raise SandboxError("E2B sandbox backend requires a template id")
        self.spec = spec
        self.sandbox_cls = sandbox_cls
        self.connection = (
            ExplicitE2BConnection.model_validate(connection.model_dump())
            if connection is not None else None
        )

    def _get_sandbox_cls(self) -> Any:
        sandbox_cls = self.sandbox_cls
        if sandbox_cls is not None:
            return sandbox_cls
        try:
            from e2b import Sandbox  # type: ignore[import-not-found, import-untyped]
        except ImportError as exc:
            raise SandboxError(
                "e2b>=2.15.3,<2.25.0 is required for KSADK_SANDBOX_BACKEND=e2b"
            ) from exc
        return Sandbox
    def check_available(self) -> None:
        """Validate local SDK/connection configuration without creating a sandbox."""
        if self.connection is not None:
            self.connection.sdk_options()
        self._sandbox_class()

    def _sandbox_class(self):
        sandbox_cls = self.sandbox_cls
        if sandbox_cls is None:
            try:
                from e2b import Sandbox  # type: ignore[import-not-found, import-untyped]
            except ImportError as exc:
                raise SandboxError(
                    "e2b>=2.15.3,<2.25.0 is required for KSADK_SANDBOX_BACKEND=e2b"
                ) from exc
            sandbox_cls = Sandbox
        return sandbox_cls

    def create_session(
        self,
        *,
        session_id: str,
        env: dict[str, str] | None = None,
        input_files: list[SandboxInputFile] | None = None,
    ) -> SandboxSession:
        connection_options = self.connection.sdk_options() if self.connection is not None else {}
        sandbox_cls = self._sandbox_class()

        metadata = {
            "runtime": "ksadk",
            "sandbox_type": self.spec.sandbox_type.value,
            **self.spec.metadata,
            "session_id": session_id,
        }
        runtime_env = {**self.spec.env, **(env or {})}
        sandbox = sandbox_cls.create(
            template=self.spec.template_id,
            timeout=self.spec.timeout,
            metadata=metadata,
            envs=runtime_env,
            allow_internet_access=self.spec.allow_internet_access,
            **connection_options,
        )
        session = self._wrap_sandbox(sandbox)
        try:
            self._wait_until_ready(session, runtime_env)
            session.write_files(
                [(item.target_path, item.source.read_bytes()) for item in input_files or []]
            )
        except BaseException:
            try:
                session.kill()
            except Exception:
                logger.warning("E2B sandbox cleanup failed after initialization failure")
            raise
        return session

    def reconnect_session(self, *, session_locator: str) -> SandboxSession:
        """Reconnect to an existing E2B sandbox without recreating it."""

        locator = session_locator.strip()
        if not locator:
            raise SandboxError("E2B reconnect requires a sandbox ID")
        sandbox_cls = self._get_sandbox_cls()
        connection_options = self.connection.sdk_options() if self.connection is not None else {}
        sandbox = sandbox_cls.connect(locator, timeout=self.spec.timeout, **connection_options)
        session = self._wrap_sandbox(sandbox)
        _with_startup_retry(lambda: session.run_command("true"))
        return session

    def _wrap_sandbox(self, sandbox: Any) -> E2BSandboxSession:
        return E2BSandboxSession(sandbox)

    def _wait_until_ready(
        self, session: E2BSandboxSession, env: dict[str, str] | None = None
    ) -> None:
        _with_startup_retry(lambda: session.run_command("true"))
        _with_startup_retry(lambda: session.write_file("/tmp/.ksadk-sandbox-ready", ""))
        self._wait_env_applied(session, env or {})

    def _wait_env_applied(self, session: E2BSandboxSession, env: dict[str, str]) -> None:
        # create 返回后 envd 异步应用 envVars, 实测存在秒级延迟;
        # 轮询一个非空变量直到值就位, 避免紧随其后的 agent 进程读到缺失的环境。
        probe = next(((k, v) for k, v in env.items() if v), None)
        if probe is None:
            return
        key, expected = probe
        attempts = _startup_retry_attempts()
        delay = _startup_retry_delay()
        for attempt in range(attempts):
            result = session.run_command(f"printenv {key} || true")
            if result.stdout.strip() == expected:
                return
            time.sleep(min(delay * (2**attempt), 1.0))
        # 不 raise: 模板可能预置同名空值覆盖 create 注入 (envd 合并优先级问题),
        # 此时调用方仍可通过 run_command(env=...) 的 per-command 注入拿到正确值。
        logger.warning(
            "E2B sandbox env %s not applied after %d attempts (expected %r); "
            "template may predefine an empty override",
            key,
            attempts,
            expected,
        )
