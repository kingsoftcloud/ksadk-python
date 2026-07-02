from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from ksadk.sandbox.base import SandboxCommandResult, SandboxInputFile, SandboxSession


class LocalProcessSandboxSession:
    def __init__(self, *, session_id: str, workspace_root: Path, backend_name: str = "local_process"):
        self._session_id = session_id
        self._workspace_root = workspace_root.expanduser().resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._backend_name = backend_name

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
        process_env = self._allowed_env(env or {})
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=resolved_cwd,
                env=process_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout)
            return SandboxCommandResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
            )
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass
            stdout, stderr = process.communicate()
            return SandboxCommandResult(
                stdout=stdout or exc.stdout or "",
                stderr=(stderr or exc.stderr or "") + "\ncommand timed out",
                exit_code=124,
            )

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
        return resolved

    def _resolve_workspace_path(self, path: str) -> Path:
        raw = str(path or "").strip().lstrip("/") or "."
        target = (self._workspace_root / raw).resolve()
        if target != self._workspace_root and self._workspace_root not in target.parents:
            raise ValueError("path must stay inside the sandbox workspace")
        return target

    @staticmethod
    def _allowed_env(env: dict[str, str]) -> dict[str, str]:
        allowed = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        for key, value in env.items():
            if key.startswith("KSADK_COMMAND_"):
                continue
            if key.startswith(("KSADK_SAFE_", "PYTHON", "NODE", "UV_")):
                allowed[key] = str(value)
        return allowed


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
