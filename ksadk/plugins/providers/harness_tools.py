"""Provider-owned execution of locked Python and SDK builtin tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ksadk.harness.tools import HarnessTool


def python_tool_bundle_path(digest: str) -> Path:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Python Tool requires a locked source SHA-256")
    return Path("capabilities/tools") / f"{digest[7:]}.py"


_EXECUTE = """
import asyncio, contextlib, importlib.util, inspect, json, sys
path, name = sys.argv[1:]
with contextlib.redirect_stdout(sys.stderr):
    spec = importlib.util.spec_from_file_location('locked_tool', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = getattr(module, name)(**json.load(sys.stdin))
    if inspect.isawaitable(value):
        value = asyncio.run(value)
print(json.dumps(value, ensure_ascii=False))
"""

_EXECUTE_BUILTIN = """
import contextlib, json, sqlite3, sys
from dataclasses import asdict
sys.path.insert(0, sys.argv[1])
with contextlib.redirect_stdout(sys.stderr):
    from pathlib import Path
    from ksadk.toolsets import get_agentengine_tools
    import ksadk.toolsets.workspace as workspace
    import ksadk.toolsets.workspace_state as state
    workspace.workspace_root = lambda: Path(sys.argv[2])
    tool = get_agentengine_tools(include=[sys.argv[3]], mode='direct')[0]
    arguments = json.load(sys.stdin)
    # Keep read-before-edit protection across short-lived executor processes.
    # The DB is outside the tool-visible workspace and scoped by session.
    db_path = Path(sys.argv[4])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS reads (session TEXT PRIMARY KEY, data TEXT)')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM reads WHERE session=?', (sys.argv[5],)).fetchone()
        if row:
            for item in json.loads(row[0]):
                state.record_read_state(state.WorkspaceReadState(**item))
        value = tool.invoke(arguments) if hasattr(tool, 'invoke') else tool(**arguments)
        snapshot = [asdict(item) for item in state._READ_STATE.values()]
        db.execute('INSERT OR REPLACE INTO reads VALUES (?, ?)',
                   (sys.argv[5], json.dumps(snapshot)))
print(json.dumps(value, ensure_ascii=False))
"""


def _supervised_script(script: str) -> str:
    """Stop the owned process group if its supervisor dies, including SIGKILL.

    Cooperative cleanup in the parent cannot run after a hard kill. This is
    lifecycle cleanup for trusted host tools, not a sandbox or an undo promise.
    """
    return (
        f"""
import os, signal, threading, time
_supervisor_pid = {os.getpid()}
def _check_supervisor():
    if os.getppid() != _supervisor_pid:
        os.killpg(os.getpgrp(), signal.SIGKILL)
def _watch_supervisor():
    while True:
        _check_supervisor()
        time.sleep(0.05)
_check_supervisor()
threading.Thread(target=_watch_supervisor, daemon=True).start()
"""
        + script
    )


def validate_tool_executor(contract: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate the same executor contract at build time and provider activation."""
    name = str(contract.get("name", ""))
    executor = contract.get("executor", "builtin")
    if executor == "python":
        return None
    if executor != "builtin":
        raise ValueError(
            f"Tool {name} 的执行类型 {executor} 尚不支持直接绑定到 KsADK Harness；"
            "MCP 工具请通过 MCP Server 绑定，延迟工具请绑定具体工具。"
        )
    # Unrestricted dispatchers can reach tools outside the Agent's locked bindings.
    if name in {"tool_dispatcher", "agentengine_tool_dispatcher", "tool_search"}:
        raise ValueError(f"Tool {name} 不能绕过绑定范围；请绑定需要执行的具体工具")
    from ksadk.toolsets import describe_agentengine_tools

    try:
        descriptors = describe_agentengine_tools(include=[name], mode="direct")
    except ValueError as exc:
        raise ValueError(f"Tool {name} 没有可用的 SDK 内置实现") from exc
    if len(descriptors) != 1 or descriptors[0]["name"] != name:
        raise ValueError(f"Tool {name} 必须引用具体工具，不能引用工具组")
    if not descriptors[0].get("enabled", True):
        raise ValueError(f"Tool {name} 的执行后端尚未配置，请配置后端或取消绑定")
    return descriptors[0]


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def assemble_python_tools(
    root: Path,
    resolved: dict[str, Any],
    *,
    workspace_root: Path | None = None,
    mcp_server_names: frozenset[str] = frozenset(),
):
    """Load descriptors only; source code runs after the engine's approval gate."""
    tools: dict[str, HarnessTool] = {}
    approvals: set[str] = set()
    granted = set(resolved.get("security", {}).get("allowedPermissions", []))
    bound_mcp = {
        item.get("name")
        for item in resolved.get("capabilities", {}).get("mcpServers", ())
        if item.get("enabled", True)
    }
    for contract in resolved.get("capabilities", {}).get("tools", []):
        contract = _plain(contract)
        if not contract.get("enabled", True):
            continue
        if (
            contract.get("executor") == "mcp"
            and contract.get("mcpServer") in bound_mcp & mcp_server_names
        ):
            # Executed by the projected MCP runtime, never by the host Python dispatcher.
            continue
        descriptor = validate_tool_executor(contract)
        if descriptor is None and "process:host-user" not in granted:
            raise ValueError("Python Tool requires explicit host execution permission")
        if set(contract.get("permissions", [])) - granted:
            raise ValueError("Python Tool requests permissions not granted by this Agent")
        path = None
        if descriptor is None:
            digest = str(contract.get("sourceSha256") or "")
            path = (root / python_tool_bundle_path(digest)).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError("Python Tool source is missing from the locked Bundle")
            if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Python Tool source digest mismatch")
        name = str(contract["name"])
        if name in tools:
            raise ValueError("Duplicate Python Tool name")
        if (
            contract.get("approval") != "never"
            or contract.get("sideEffect")
            in {
                "write",
                "external",
            }
            or (
                descriptor
                and (descriptor.get("side_effects") or descriptor.get("requires_approval"))
            )
        ):
            approvals.add(name)

        async def invoke(
            arguments, call_id, *, path=path, contract=contract, descriptor=descriptor
        ):
            del call_id
            env = {}
            execution_root = root
            if descriptor is None:
                command = [_EXECUTE, str(path), contract["callableName"]]
            else:
                # Trusted SDK code only. Approval is owned by the Managed Loop;
                # do not ask a second time inside the builtin gateway.
                from ksadk.harness.execution_policy import current_execution_policy
                from ksadk.runtime_context import get_current_tool_execution_context_or_default
                from ksadk.toolsets.workspace_identity import identity_workspace_root

                policy = current_execution_policy()
                effective_workspace = (
                    policy.workspace_root
                    if policy and policy.workspace_root is not None
                    else workspace_root or root
                )
                execution_root = effective_workspace.resolve()
                execution_root.mkdir(parents=True, exist_ok=True)
                env = dict(
                    os.environ,
                    KSADK_TOOL_APPROVAL_MODE="full",
                    KSADK_PROJECT_DIR=str(execution_root),
                    AGENTENGINE_UI_DIR=str(execution_root / ".harness-tools" / "ui"),
                )
                # The host policy already supplies the authorized Run scope.
                # Keep relative file paths identical across builtin and host
                # artifact tools; a second nested scope breaks that contract.
                scoped_root = (
                    execution_root
                    if policy and policy.workspace_root is not None
                    else identity_workspace_root(
                        effective_workspace / ".harness-tools" / "workspace"
                    ).resolve()
                )
                command = [
                    _EXECUTE_BUILTIN,
                    str(Path(__file__).resolve().parents[3]),
                    str(scoped_root),
                    contract["name"],
                    str(
                        effective_workspace
                        / ".harness-tools"
                        / "state"
                        / (hashlib.sha256(str(scoped_root).encode()).hexdigest() + ".sqlite")
                    ),
                    get_current_tool_execution_context_or_default().session_id or "default",
                ]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                "-I",
                "-c",
                _supervised_script(command[0]),
                *command[1:],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=execution_root,
                env=env,
                start_new_session=True,
            )
            async def exchange():
                process.stdin.write(json.dumps(arguments).encode())
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > 1_048_576:
                        raise ValueError("Tool 输出超过 1 MiB 限制")
                await process.wait()
                if process.returncode:
                    raise RuntimeError(
                        f"Tool {contract['name']} 执行失败，请检查参数与运行环境"
                    )
                return json.loads(output)

            try:
                return await asyncio.wait_for(
                    exchange(), timeout=contract.get("timeoutSeconds", 20)
                )
            finally:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

        tools[name] = HarnessTool(
            name=name,
            description=contract.get("description", ""),
            parameters=contract.get("inputSchema", {}),
            handler=invoke,
            source="sdk-builtin" if descriptor else "locked-python",
        )
    return tools, approvals
