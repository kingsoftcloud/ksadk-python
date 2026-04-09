from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

from ksadk.sandbox.base import (
    BaseSandbox,
    ExecutionConfig,
    ExecutionResult,
    ExecutionStatus,
    Language,
    language_extension,
)
from ksadk.sandbox.security import SecurityPolicy


class LocalCodeSandbox(BaseSandbox):
    def __init__(
        self,
        base_dir: str | None = None,
        default_config: ExecutionConfig | None = None,
        security_policy: SecurityPolicy | None = None,
    ):
        super().__init__(default_config=default_config)
        self._owns_base_dir = base_dir is None
        self.base_dir = base_dir or tempfile.mkdtemp(prefix="ksadk_sandbox_")
        Path(self.base_dir).mkdir(parents=True, exist_ok=True)
        self.security_policy = security_policy or SecurityPolicy()
        self._created_dirs: list[str] = []

    async def execute(
        self,
        code: str,
        language: Language = Language.PYTHON,
        config: ExecutionConfig | None = None,
    ) -> ExecutionResult:
        config = self.resolve_config(config)
        violation = self.security_policy.validate(code, language=language, config=config)
        if violation:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                stderr=f"Security violation: {violation}",
            )

        start_time = time.monotonic()

        try:
            work_dir = self._create_work_dir(config)
            script_path = self._write_script(work_dir, code, language)
            baseline_files = self._snapshot_files(work_dir)
            command = self._build_command(language, script_path)
            environment = self._build_env(config, work_dir)
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                stderr=f"Execution setup failed: {exc}",
                execution_time_ms=(time.monotonic() - start_time) * 1000,
            )

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=environment,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                self._kill_process(process)
                stdout, stderr = await process.communicate()
                stdout_text, stdout_truncated = self._truncate_output(
                    stdout,
                    config.max_output_bytes,
                )
                stderr_text, stderr_truncated = self._truncate_output(
                    stderr,
                    config.max_output_bytes,
                )
                return ExecutionResult(
                    status=ExecutionStatus.TIMEOUT,
                    stdout=stdout_text,
                    stderr=stderr_text
                    or f"Execution timed out after {config.timeout_seconds}s",
                    execution_time_ms=(time.monotonic() - start_time) * 1000,
                    files_created=self._detect_created_files(work_dir, baseline_files),
                    metadata={
                        "return_code": process.returncode,
                        "stdout_truncated": stdout_truncated,
                        "stderr_truncated": stderr_truncated,
                    },
                )

            stdout_text, stdout_truncated = self._truncate_output(stdout, config.max_output_bytes)
            stderr_text, stderr_truncated = self._truncate_output(stderr, config.max_output_bytes)
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS
                if process.returncode == 0
                else ExecutionStatus.ERROR,
                stdout=stdout_text,
                stderr=stderr_text,
                execution_time_ms=(time.monotonic() - start_time) * 1000,
                files_created=self._detect_created_files(work_dir, baseline_files),
                metadata={
                    "return_code": process.returncode,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "language": language.value,
                },
            )
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                stderr=f"Execution failed: {exc}",
                execution_time_ms=(time.monotonic() - start_time) * 1000,
            )

    async def cleanup(self) -> None:
        for directory in reversed(self._created_dirs):
            shutil.rmtree(directory, ignore_errors=True)
        self._created_dirs.clear()

        if self._owns_base_dir:
            shutil.rmtree(self.base_dir, ignore_errors=True)

    def _create_work_dir(self, config: ExecutionConfig) -> str:
        root_dir = config.working_dir or self.base_dir
        Path(root_dir).mkdir(parents=True, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="run_", dir=root_dir)
        self._created_dirs.append(work_dir)
        return work_dir

    def _write_script(self, work_dir: str, code: str, language: Language) -> Path:
        script_path = Path(work_dir) / f"script{language_extension(language)}"
        script_path.write_text(code, encoding="utf-8")
        return script_path

    def _build_command(self, language: Language, script_path: Path) -> list[str]:
        if language is Language.PYTHON:
            executable = sys.executable or shutil.which("python3")
        elif language is Language.BASH:
            executable = shutil.which("bash") or "/bin/bash"
        elif language is Language.JAVASCRIPT:
            executable = shutil.which("node")
        else:
            executable = None

        if not executable:
            raise RuntimeError(f"No executable available for {language.value}")

        if language is Language.PYTHON:
            return [executable, "-u", str(script_path)]
        return [executable, str(script_path)]

    def _build_env(self, config: ExecutionConfig, work_dir: str) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = work_dir
        env["TMPDIR"] = work_dir
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        if not config.allow_network:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
                env[key] = ""
            env["NO_PROXY"] = "*"
            env["no_proxy"] = "*"

        return env

    def _snapshot_files(self, work_dir: str) -> set[str]:
        root = Path(work_dir)
        return {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }

    def _detect_created_files(self, work_dir: str, baseline_files: set[str]) -> list[str]:
        current_files = self._snapshot_files(work_dir)
        return sorted(current_files - baseline_files)

    def _truncate_output(self, data: bytes, limit: int) -> tuple[str, bool]:
        if len(data) <= limit:
            return data.decode("utf-8", errors="replace"), False

        truncated = data[:limit].decode("utf-8", errors="ignore")
        return f"{truncated}\n...[truncated]", True

    def _kill_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            process.kill()
