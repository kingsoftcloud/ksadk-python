"""Invocation-owned execution policy for Harness sandbox tools."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any


class SandboxPolicyDenied(PermissionError):
    """Raised before execution when a Harness sandbox action violates policy."""


_READ_COMMANDS = frozenset({"cat", "head", "ls", "pwd", "stat", "tail", "wc"})
_NO_PATH_COMMANDS = frozenset({"pwd"})
_REQUIRE_PATH_COMMANDS = frozenset({"cat", "head", "stat", "tail", "wc"})
_VALUE_OPTIONS = frozenset({"-n", "--bytes", "--lines", "--max-unchanged-stats"})
_SHELL_OPERATORS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"})


class HarnessSandboxExecutor:
    """Run a deliberately small read-only command surface inside one workspace.

    Commands are executed without a shell. Path operands are resolved before the
    child process starts, including symlinks, so reads cannot escape the configured
    workspace. The narrow command surface also excludes network clients and process
    launchers entirely.
    """

    def __init__(self, *, workspace_root: str | Path, read_only: bool = True) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._read_only = read_only

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def read_file(self, path: str) -> dict[str, Any]:
        target = self._resolve_path(path)
        content = await asyncio.to_thread(target.read_text, encoding="utf-8")
        return {
            "ok": True,
            "path": target.relative_to(self._workspace_root).as_posix(),
            "content": content,
            "policy": "read-only",
        }

    async def run_command(self, command: str) -> dict[str, Any]:
        argv = self._validate_command(command)
        completed = await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=self._workspace_root,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "command": " ".join(argv),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
            "policy": "read-only",
        }

    def _validate_command(self, command: str) -> list[str]:
        try:
            argv = shlex.split(str(command or ""), posix=True)
        except ValueError as exc:
            raise SandboxPolicyDenied(f"read-only sandbox denied malformed command: {exc}") from exc
        if not argv:
            raise SandboxPolicyDenied("read-only sandbox denied empty command")
        if any(argument in _SHELL_OPERATORS for argument in argv):
            raise SandboxPolicyDenied(
                "read-only sandbox denied shell redirection, pipelines, or command chaining"
            )
        executable = argv[0]
        if executable not in _READ_COMMANDS:
            raise SandboxPolicyDenied(
                f"read-only sandbox denied command {executable!r}; network, write, and "
                "dangerous process actions are not allowed"
            )
        if executable in _NO_PATH_COMMANDS:
            if len(argv) != 1:
                raise SandboxPolicyDenied(
                    f"read-only sandbox denied arguments for command {executable!r}"
                )
            return argv

        normalized = [executable]
        skip_option_value = False
        saw_path = False
        for argument in argv[1:]:
            if skip_option_value:
                normalized.append(argument)
                skip_option_value = False
                continue
            if argument == "--":
                normalized.append(argument)
                continue
            if argument in _VALUE_OPTIONS:
                normalized.append(argument)
                skip_option_value = True
                continue
            if argument.startswith("-"):
                normalized.append(argument)
                continue
            target = self._resolve_path(argument)
            normalized.append(str(target))
            saw_path = True
        if executable in _REQUIRE_PATH_COMMANDS and not saw_path:
            raise SandboxPolicyDenied(
                f"read-only sandbox denied command {executable!r} without a workspace path"
            )
        return normalized

    def _resolve_path(self, path: str) -> Path:
        raw = str(path or "").strip()
        if not raw:
            raise SandboxPolicyDenied("read-only sandbox requires a workspace path")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._workspace_root / candidate
        resolved = candidate.expanduser().resolve()
        if resolved != self._workspace_root and self._workspace_root not in resolved.parents:
            raise SandboxPolicyDenied(f"read-only sandbox denied path outside workspace: {raw!r}")
        return resolved


__all__ = ["HarnessSandboxExecutor", "SandboxPolicyDenied"]
