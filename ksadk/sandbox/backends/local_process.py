from __future__ import annotations

import os
from pathlib import Path

from ksadk.sandbox.base import SandboxCommandResult, SandboxInputFile, SandboxSession
from ksadk.sandbox.local_controls import (
    LocalControlSettings,
    build_script_environment,
    check_command,
    resource_limit_reason,
    run_bounded_process,
)


class LocalProcessSandboxSession:
    def __init__(
        self, *, session_id: str, workspace_root: Path, backend_name: str = "local_process"
    ):
        self._session_id = session_id
        self._workspace_root = workspace_root.expanduser().resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._backend_name = backend_name
        self._controls = LocalControlSettings.from_trusted_host_env()

    @property
    def sandbox_id(self) -> str:
        return f"{self._backend_name}:{self._session_id}"

    def write_file(self, path: str, data: str | bytes) -> None:
        target = self._resolve_workspace_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")

    def read_file(self, path: str) -> str:
        return self._resolve_workspace_path(path).read_text(encoding="utf-8")

    def read_file_bytes(self, path: str, *, max_bytes: int) -> bytes:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("File download limit must be positive")
        with self._resolve_workspace_path(path).open("rb") as source:
            content = source.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("Sandbox file exceeds download limit")
        return content

    def run_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> SandboxCommandResult:
        try:
            resolved_cwd = self._resolve_cwd(cwd or (env or {}).get("KSADK_COMMAND_CWD"))
        except ValueError as exc:
            return SandboxCommandResult(stderr=str(exc), exit_code=126)
        finding = check_command(
            command,
            cwd=resolved_cwd,
            allowed_roots=(self._workspace_root,),
        )
        if finding is not None:
            return SandboxCommandResult(
                stderr=f"blocked by local-process policy: {finding.reason()}", exit_code=126
            )
        source_env = dict(os.environ)
        source_env.update(env or {})
        process_env = build_script_environment(source_env, settings=self._controls)
        try:
            effective_timeout = self._controls.wall_seconds
            if timeout is not None:
                effective_timeout = min(timeout, effective_timeout)
            executed = run_bounded_process(
                ["/bin/sh", "-c", command],
                cwd=resolved_cwd,
                env=process_env,
                timeout=effective_timeout,
                settings=self._controls,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return SandboxCommandResult(
                stderr=f"local-process controls failed: {exc}", exit_code=125
            )
        diagnostics: list[str] = []
        if executed.controls.get("status") == "failed":
            diagnostics.append("resource limits were not applied")
        if executed.output_limit_exceeded:
            diagnostics.append("command output limit exceeded")
        if executed.timed_out:
            diagnostics.append("command timed out")
        if limit := resource_limit_reason(executed.exit_code):
            diagnostics.append(f"command exceeded {limit} resource limit")
        if executed.cleanup_error:
            diagnostics.append("command process-group cleanup failed")
        stderr = executed.stderr
        if diagnostics:
            stderr = (stderr.rstrip() + "\n" if stderr else "") + "; ".join(diagnostics)
        exit_code = executed.exit_code
        if executed.timed_out:
            exit_code = 124
        elif executed.output_limit_exceeded:
            exit_code = 122
        elif executed.controls.get("status") == "failed" or executed.cleanup_error:
            exit_code = 125
        return SandboxCommandResult(stdout=executed.stdout, stderr=stderr, exit_code=exit_code)

    def get_host(self, port: int) -> str:
        return f"http://127.0.0.1:{int(port)}"

    def kill(self) -> None:
        return None

    def _resolve_cwd(self, cwd: str | None) -> Path:
        if not cwd:
            return self._workspace_root
        candidate = Path(cwd)
        if not candidate.is_absolute():
            candidate = self._workspace_root / candidate
        resolved = candidate.expanduser().resolve()
        if resolved != self._workspace_root and self._workspace_root not in resolved.parents:
            raise ValueError("cwd must stay inside the sandbox workspace")
        self._reject_symlink_components(candidate)
        return resolved

    def _resolve_workspace_path(self, path: str) -> Path:
        raw = str(path or "").strip().lstrip("/") or "."
        self._reject_symlink_components(self._workspace_root / raw)
        target = (self._workspace_root / raw).resolve()
        if target != self._workspace_root and self._workspace_root not in target.parents:
            raise ValueError("path must stay inside the sandbox workspace")
        return target

    def _reject_symlink_components(self, path: Path) -> None:
        try:
            relative = path.absolute().relative_to(self._workspace_root)
        except ValueError:
            raise ValueError("path must stay inside the sandbox workspace") from None
        current = self._workspace_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("sandbox file API does not follow symbolic links")


class LocalProcessSandboxBackend:
    isolated = False

    def __init__(self, *, workspace_root: Path, backend_name: str = "local_process"):
        self.workspace_root = workspace_root
        self.backend_name = backend_name

    def create_session(
        self,
        *,
        session_id: str,
        env: dict[str, str] | None = None,
        input_files: list[SandboxInputFile] | None = None,
    ) -> SandboxSession:
        session = LocalProcessSandboxSession(
            session_id=session_id,
            workspace_root=self.workspace_root,
            backend_name=self.backend_name,
        )
        for item in input_files or []:
            session.write_file(item.target_path, item.source.read_bytes())
        return session
