from __future__ import annotations

import os
import shlex
import time
import logging
from typing import Any
from uuid import uuid4

from ksadk.runtime_context import get_current_tool_execution_context_or_default
from ksadk.sandbox import SandboxError, SandboxInputFile, create_sandbox_backend
from ksadk.sandbox.registry import GLOBAL_SANDBOX_REGISTRY
from ksadk.tools.gateway import ToolPolicy, check_command_policy, default_tool_gateway
from ksadk.tools.result_budget import budget_text_fields
from ksadk.toolsets._langchain import as_tool
from ksadk.toolsets.workspace import workspace_root


logger = logging.getLogger(__name__)


_SANDBOX_TOOL_POLICIES = {
    "sandbox_status": ToolPolicy(risk_level="low"),
    "run_command": ToolPolicy(risk_level="high", side_effects=("sandbox_command_execution",)),
    "run_code": ToolPolicy(risk_level="high", side_effects=("sandbox_code_execution",)),
}


def _gateway():
    return default_tool_gateway(_SANDBOX_TOOL_POLICIES)


def sandbox_backend_name() -> str:
    explicit = os.environ.get("KSADK_SANDBOX_BACKEND", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID"):
        return "e2b"
    return "none"


def _sandbox_execution_backend_name() -> str:
    explicit = os.environ.get("KSADK_SANDBOX_BACKEND", "").strip().lower()
    return explicit or "e2b"


def sandbox_status() -> dict:
    """Report configured AgentEngine sandbox status and boundaries."""

    return _gateway().invoke("sandbox_status", _sandbox_status_impl)


def _sandbox_status_impl() -> dict[str, Any]:
    backend = sandbox_backend_name()
    timeout = int(os.environ.get("KSADK_SANDBOX_TIMEOUT") or os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT") or "900")
    idle_ttl = _int_env("KSADK_SANDBOX_IDLE_TTL_SECONDS", 0)
    max_sessions = _int_env("KSADK_SANDBOX_MAX_SESSIONS", 0)
    template_id = os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID") or ""
    isolated = backend not in {"local", "local_process", "pod", "pod_process"}
    latest = GLOBAL_SANDBOX_REGISTRY.latest()
    now = time.time()
    return {
        "ok": True,
        "backend": backend,
        "enabled": backend not in {"", "disabled", "none", "off"},
        "isolated": isolated,
        "template_bound": bool(template_id),
        "template_id": template_id,
        "timeout_seconds": timeout,
        "ttl_seconds": _sandbox_ttl_seconds(),
        "idle_ttl_seconds": idle_ttl,
        "max_sessions": max_sessions,
        "sandbox_id": latest.sandbox_id if latest else "",
        "created_at": latest.created_at if latest else None,
        "last_used_at": latest.last_used_at if latest else None,
        "expires_at": latest.expires_at if latest else None,
        "idle_seconds": int(now - latest.last_used_at) if latest else None,
        "boundary": _sandbox_boundary(backend),
    }


def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a shell command inside the configured isolated sandbox."""

    return _gateway().invoke("run_command", _run_command_impl, command, cwd, timeout, env, background, approval=approval)


def _run_command_impl(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    background: bool = False,
) -> dict[str, Any]:
    command_text = str(command or "").strip()
    if not command_text:
        return {"ok": False, "error_message": "command is required"}
    policy = check_command_policy(command_text)
    if not policy.get("ok"):
        return policy
    if background:
        return {"ok": False, "error_type": "background_not_supported", "error_message": "background commands are not supported in P0"}
    try:
        entry, _created = _sandbox_entry("ksadk-direct")
        result = entry.session.run_command(command_text, timeout=timeout, env=env, cwd=cwd)
        payload = {
            "ok": True,
            "backend": f"sandbox/{_sandbox_execution_backend_name()}",
            "sandbox_id": entry.sandbox_id,
            "command": command_text,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
        return budget_text_fields(payload, tool_name="run_command", fields=("stdout", "stderr"), metadata={"tool_use_id": entry.sandbox_id})
    except SandboxError as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def run_code(
    code: str,
    language: str = "python",
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a code snippet through the sandbox; this is not a shell replacement."""

    return _gateway().invoke("run_code", _run_code_impl, code, language, timeout, env, approval=approval)


def _run_code_impl(
    code: str,
    language: str = "python",
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = str(code or "")
    if not source:
        return {"ok": False, "error_message": "code is required"}
    backend_name = _sandbox_execution_backend_name()
    if not _sandbox_backend_isolated(backend_name):
        return {
            "ok": False,
            "error_type": "isolated_sandbox_required",
            "error_message": "run_code requires an isolated sandbox backend; local_process and pod_process are not supported for snippet execution",
            "backend": backend_name,
            "boundary": _sandbox_boundary(backend_name),
        }
    language_name = _normalize_language(language)
    try:
        suffix, command_prefix = _language_command(language_name)
    except ValueError as exc:
        return {"ok": False, "error_message": str(exc)}

    path = f"/tmp/ksadk-run-code-{uuid4().hex}{suffix}"
    try:
        entry, _created = _sandbox_entry("ksadk-code")
        entry.session.write_file(path, source)
        command = f"{command_prefix} {shlex.quote(path)}"
        result = entry.session.run_command(command, timeout=timeout, env=env)
        payload = {
            "ok": True,
            "backend": f"sandbox/{_sandbox_execution_backend_name()}",
            "sandbox_id": entry.sandbox_id,
            "execution_model": "snippet_runner",
            "boundary": _sandbox_boundary(_sandbox_execution_backend_name()),
            "language": language_name,
            "path": path,
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
        return budget_text_fields(payload, tool_name="run_code", fields=("stdout", "stderr"), metadata={"tool_use_id": entry.sandbox_id})
    except SandboxError as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def _normalize_language(language: str) -> str:
    value = str(language or "python").strip().lower()
    aliases = {
        "py": "python",
        "python3": "python",
        "js": "javascript",
        "node": "javascript",
        "nodejs": "javascript",
        "sh": "bash",
        "shell": "bash",
    }
    return aliases.get(value, value)


def _language_command(language: str) -> tuple[str, str]:
    if language == "python":
        return ".py", "python"
    if language == "javascript":
        return ".js", "node"
    if language == "bash":
        return ".sh", "bash"
    raise ValueError(f"unsupported language: {language}")


def _sandbox_entry(prefix: str):
    backend_name = _sandbox_execution_backend_name()
    backend = create_sandbox_backend()
    ttl = _sandbox_ttl_seconds()
    idle_ttl = _int_env("KSADK_SANDBOX_IDLE_TTL_SECONDS", 0)
    max_sessions = _int_env("KSADK_SANDBOX_MAX_SESSIONS", 0) or None
    key = _sandbox_session_key(prefix)
    isolated = _sandbox_backend_isolated(backend_name)
    input_files = _workspace_input_files() if isolated else []
    entry, created = GLOBAL_SANDBOX_REGISTRY.get_or_create(
        key=key,
        backend_name=backend_name,
        backend=backend,
        ttl_seconds=ttl,
        idle_ttl_seconds=idle_ttl,
        max_sessions=max_sessions,
        input_files=input_files,
        isolated=isolated,
    )
    logger.info(
        "ksadk.sandbox.%s",
        "create" if created else "reuse",
        extra={
            "sandbox_backend": backend_name,
            "sandbox_id": entry.sandbox_id,
            "isolated": isolated,
            "workspace_input_files": len(input_files) if created else 0,
        },
    )
    return entry, created


def _sandbox_session_key(prefix: str) -> str:
    explicit = os.environ.get("KSADK_SANDBOX_SESSION_ID")
    if explicit:
        return explicit
    context = get_current_tool_execution_context_or_default()
    if context.session_id:
        return f"ksadk-session:{context.session_id}"
    return f"{prefix}-shared"


def _sandbox_backend_isolated(backend_name: str) -> bool:
    return backend_name not in {"local", "local_process", "pod", "pod_process"}


def _workspace_input_files() -> list[SandboxInputFile]:
    root = workspace_root()
    if not root.exists():
        return []
    max_files = _int_env("KSADK_SANDBOX_SYNC_MAX_FILES", 2000)
    max_file_bytes = _int_env("KSADK_SANDBOX_SYNC_MAX_FILE_BYTES", 2 * 1024 * 1024)
    max_total_bytes = _int_env("KSADK_SANDBOX_SYNC_MAX_TOTAL_BYTES", 25 * 1024 * 1024)
    skipped_dirs = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
    files: list[SandboxInputFile] = []
    total_bytes = 0
    for item in sorted(root.rglob("*")):
        if len(files) >= max_files:
            break
        if any(part in skipped_dirs for part in item.relative_to(root).parts):
            continue
        if not item.is_file():
            continue
        size = item.stat().st_size
        if size > max_file_bytes or total_bytes + size > max_total_bytes:
            continue
        relative = item.relative_to(root).as_posix()
        files.append(SandboxInputFile(source=item, target_path=f"/workspace/{relative}"))
        total_bytes += size
    return files


def _sandbox_ttl_seconds() -> int:
    return _int_env("KSADK_SANDBOX_TTL_SECONDS", _int_env("KSADK_SANDBOX_TIMEOUT", 900))


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    try:
        return max(0, int(raw))
    except ValueError:
        return int(default)


def _sandbox_boundary(backend: str) -> str:
    if backend in {"local", "local_process"}:
        return "executes as a local child process; shares local filesystem, env allowlist, and network"
    if backend in {"pod", "pod_process"}:
        return "executes inside the agent pod; shares pod filesystem, env allowlist, network, and service account"
    return "Sandbox tools execute commands and code only through the configured isolated sandbox backend; they never expose the host shell."


def get_sandbox_tools() -> list:
    return [as_tool(sandbox_status), as_tool(run_command), as_tool(run_code)]
