"""Provider-owned execution of locked Python Tool sources."""

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


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def assemble_python_tools(root: Path, resolved: dict[str, Any]):
    """Load descriptors only; source code runs after the engine's approval gate."""
    tools: dict[str, HarnessTool] = {}
    approvals: set[str] = set()
    granted = set(resolved.get("security", {}).get("allowedPermissions", []))
    for contract in resolved.get("capabilities", {}).get("tools", []):
        contract = _plain(contract)
        if not contract.get("enabled", True):
            continue
        if contract.get("executor", "builtin") != "python":
            raise ValueError("This Harness Provider cannot execute the selected Tool executor")
        if "process:host-user" not in granted:
            raise ValueError("Python Tool requires explicit host execution permission")
        if set(contract.get("permissions", [])) - granted:
            raise ValueError("Python Tool requests permissions not granted by this Agent")
        digest = str(contract.get("sourceSha256") or "")
        path = (root / python_tool_bundle_path(digest)).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("Python Tool source is missing from the locked Bundle")
        if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Python Tool source digest mismatch")
        name = str(contract["name"])
        if name in tools:
            raise ValueError("Duplicate Python Tool name")
        if contract.get("approval") != "never" or contract.get("sideEffect") in {
            "write",
            "external",
        }:
            approvals.add(name)

        async def invoke(arguments, call_id, *, path=path, contract=contract):
            del call_id
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                "-I",
                "-c",
                _EXECUTE,
                str(path),
                contract["callableName"],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=root,
                env={},
                start_new_session=True,
            )
            try:
                async with asyncio.timeout(contract.get("timeoutSeconds", 20)):
                    process.stdin.write(json.dumps(arguments).encode())
                    await process.stdin.drain()
                    process.stdin.close()
                    output = bytearray()
                    while chunk := await process.stdout.read(65536):
                        output.extend(chunk)
                        if len(output) > 1_048_576:
                            raise ValueError("Python Tool output exceeds 1 MiB")
                    await process.wait()
                    if process.returncode:
                        raise RuntimeError("Python Tool process failed")
                    return json.loads(output)
            finally:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

        tools[name] = HarnessTool(
            name=name,
            description=contract.get("description", ""),
            parameters=contract.get("inputSchema", {}),
            handler=invoke,
            source="locked-python",
        )
    return tools, approvals
