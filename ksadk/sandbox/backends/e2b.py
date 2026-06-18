from __future__ import annotations

import os
import time
from typing import Any

from ksadk.sandbox.base import (
    SandboxCommandResult,
    SandboxError,
    SandboxInputFile,
    SandboxSession,
    SandboxSpec,
)

_TRANSIENT_STARTUP_ERROR_NAMES = {
    "NotFoundException",
    "FileNotFoundException",
    "SandboxNotFoundException",
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


class E2BSandboxSession:
    def __init__(self, sandbox: Any):
        self._sandbox = sandbox

    @property
    def sandbox_id(self) -> str:
        return str(getattr(self._sandbox, "sandbox_id", "") or "")

    def write_file(self, path: str, data: str | bytes) -> None:
        self._sandbox.files.write(path, data)

    def read_file(self, path: str) -> str:
        return str(self._sandbox.files.read(path))

    def run_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxCommandResult:
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if env is not None:
            kwargs["envs"] = env
        result = self._sandbox.commands.run(command, **kwargs)
        return SandboxCommandResult(
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
            exit_code=getattr(result, "exit_code", None),
        )

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
    ):
        if not spec.template_id:
            raise SandboxError("E2B sandbox backend requires a template id")
        self.spec = spec
        self.sandbox_cls = sandbox_cls

    def create_session(
        self,
        *,
        session_id: str,
        env: dict[str, str] | None = None,
        input_files: list[SandboxInputFile] | None = None,
    ) -> SandboxSession:
        sandbox_cls = self.sandbox_cls
        if sandbox_cls is None:
            try:
                from e2b import Sandbox
            except ImportError as exc:
                raise SandboxError("e2b>=2.0.0 is required for KSADK_SANDBOX_BACKEND=e2b") from exc
            sandbox_cls = Sandbox

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
        )
        session = self._wrap_sandbox(sandbox)
        self._wait_until_ready(session)
        for item in input_files or []:
            session.write_file(item.target_path, item.source.read_bytes())
        return session

    def _wrap_sandbox(self, sandbox: Any) -> E2BSandboxSession:
        return E2BSandboxSession(sandbox)

    def _wait_until_ready(self, session: E2BSandboxSession) -> None:
        _with_startup_retry(lambda: session.run_command("true"))
        _with_startup_retry(lambda: session.write_file("/tmp/.ksadk-sandbox-ready", ""))
