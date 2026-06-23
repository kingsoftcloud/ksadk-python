from __future__ import annotations

import os
import shlex
from typing import Any
from uuid import uuid4

from ksadk.sandbox import SandboxError, create_sandbox_backend
from ksadk.tools.gateway import ToolPolicy, default_tool_gateway
from ksadk.toolsets._langchain import as_tool


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
    template_id = os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID") or ""
    return {
        "ok": True,
        "backend": backend,
        "enabled": backend not in {"", "disabled", "none", "off"},
        "template_bound": bool(template_id),
        "template_id": template_id,
        "timeout_seconds": timeout,
        "boundary": "Sandbox tools execute commands and code only through the configured isolated sandbox backend; they never expose the host shell.",
    }


def run_command(
    command: str,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a shell command inside the configured isolated sandbox."""

    return _gateway().invoke("run_command", _run_command_impl, command, timeout, env, approval=approval)


def _run_command_impl(
    command: str,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    command_text = str(command or "").strip()
    if not command_text:
        return {"ok": False, "error_message": "command is required"}
    session = None
    try:
        backend = create_sandbox_backend()
        session = backend.create_session(session_id=f"ksadk-direct-{uuid4().hex}", env=None, input_files=None)
        result = session.run_command(command_text, timeout=timeout, env=env)
        return {
            "ok": True,
            "backend": f"sandbox/{_sandbox_execution_backend_name()}",
            "sandbox_id": session.sandbox_id,
            "command": command_text,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
    except SandboxError as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    finally:
        if session is not None:
            session.kill()


def run_code(
    code: str,
    language: str = "python",
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write code to the sandbox and execute it through the configured sandbox backend."""

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
    language_name = _normalize_language(language)
    try:
        suffix, command_prefix = _language_command(language_name)
    except ValueError as exc:
        return {"ok": False, "error_message": str(exc)}

    session = None
    path = f"/tmp/ksadk-run-code-{uuid4().hex}{suffix}"
    try:
        backend = create_sandbox_backend()
        session = backend.create_session(session_id=f"ksadk-code-{uuid4().hex}", env=None, input_files=None)
        session.write_file(path, source)
        command = f"{command_prefix} {shlex.quote(path)}"
        result = session.run_command(command, timeout=timeout, env=env)
        return {
            "ok": True,
            "backend": f"sandbox/{_sandbox_execution_backend_name()}",
            "sandbox_id": session.sandbox_id,
            "language": language_name,
            "path": path,
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
    except SandboxError as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    finally:
        if session is not None:
            session.kill()


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


def get_sandbox_tools() -> list:
    return [as_tool(sandbox_status), as_tool(run_command), as_tool(run_code)]
