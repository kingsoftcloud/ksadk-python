from __future__ import annotations

from typing import Any

from ksadk.sandbox.base import (
    SandboxCommandResult,
    SandboxError,
    SandboxInputFile,
    SandboxSession,
    SandboxSpec,
)


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

    def run_command(self, command: str, *, timeout: int | None = None) -> SandboxCommandResult:
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
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
        session = E2BSandboxSession(sandbox)
        for item in input_files or []:
            session.write_file(item.target_path, item.source.read_bytes())
        return session
