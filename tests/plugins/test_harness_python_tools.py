import asyncio
import hashlib
import json
from types import MappingProxyType

import pytest

from ksadk.plugins.providers.harness_tools import assemble_python_tools, python_tool_bundle_path


def builtin_contract(name, **kwargs):
    return dict(
        name=name,
        executor="builtin",
        approval="never",
        sideEffect="none",
        timeoutSeconds=20,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_builtin_workspace_write_read_and_escape(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workspace = tmp_path / "agent"
    contracts = [
        builtin_contract(name)
        for name in ("write_workspace_file", "read_workspace_file", "edit_workspace_file")
    ]
    tools, approvals = assemble_python_tools(
        bundle, {"capabilities": {"tools": contracts}}, workspace_root=workspace
    )
    # SDK policy cannot be downgraded by stale/incorrect catalog metadata.
    assert approvals == {"write_workspace_file", "edit_workspace_file"}
    result = await tools["write_workspace_file"].call({"path": "probe.txt", "content": "hello"})
    assert result["ok"]
    result = await tools["read_workspace_file"].call({"path": "probe.txt"})
    assert result["ok"]
    assert "hello" in json.dumps(result)
    result = await tools["edit_workspace_file"].call(
        {"path": "probe.txt", "old_text": "hello", "new_text": "updated"}
    )
    assert result["ok"], result
    from ksadk.runtime_context import tool_execution_scope

    with tool_execution_scope("other-session"):
        result = await tools["edit_workspace_file"].call(
            {"path": "probe.txt", "old_text": "updated", "new_text": "forbidden"}
        )
        assert result["error_type"] == "file_not_read"
    assert (workspace / ".harness-tools/workspace/probe.txt").read_text() == "updated"
    assert not list(bundle.iterdir())
    with pytest.raises(RuntimeError, match="执行失败"):
        await tools["read_workspace_file"].call({"path": "../../escape.txt"})


@pytest.mark.parametrize(
    "executor,name",
    [
        ("builtin", "missing_tool"),
        ("builtin", "workspace"),
        ("builtin", "tool_dispatcher"),
        ("mcp", "remote_tool"),
        ("deferred", "anything"),
    ],
)
def test_unexecutable_bindings_fail_before_run(tmp_path, executor, name):
    with pytest.raises(ValueError):
        assemble_python_tools(
            tmp_path, {"capabilities": {"tools": [{"executor": executor, "name": name}]}}
        )


def test_disabled_unsupported_binding_is_ignored(tmp_path):
    assert assemble_python_tools(
        tmp_path,
        {"capabilities": {"tools": [{"executor": "deferred", "name": "unused", "enabled": False}]}},
    ) == ({}, set())


@pytest.mark.parametrize("enabled,projected", [(True, True), (False, True), (True, False)])
def test_mcp_projection_requires_enabled_binding_and_live_contribution(
    tmp_path, enabled, projected,
):
    resolved = {"capabilities": {
        "mcpServers": [{"name": "bound", "enabled": enabled}],
        "tools": [{"executor": "mcp", "name": "remote", "mcpServer": "bound"}],
    }}
    names = frozenset({"bound"}) if projected else frozenset()
    if enabled and projected:
        assert assemble_python_tools(tmp_path, resolved, mcp_server_names=names) == ({}, set())
    else:
        with pytest.raises(ValueError):
            assemble_python_tools(tmp_path, resolved, mcp_server_names=names)


def fixture_tool(
    tmp_path, source="def calculate(budget, actual): return (actual-budget)/budget*100"
):
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    path = tmp_path / python_tool_bundle_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    contract = dict(
        name="calculate",
        executor="python",
        sourceSha256=digest,
        callableName="calculate",
        timeoutSeconds=1,
        approval="never",
        sideEffect="none",
    )
    resolved = {
        "security": {"allowedPermissions": ["process:host-user"]},
        "capabilities": {"tools": [contract]},
    }
    return resolved, path


@pytest.mark.asyncio
async def test_locked_python_tool_real_process(tmp_path):
    resolved, _ = fixture_tool(tmp_path)
    resolved["capabilities"]["tools"][0]["inputSchema"] = MappingProxyType(
        {
            "type": "object",
            "properties": MappingProxyType({"budget": {"type": "number"}}),
        }
    )
    tools, approvals = assemble_python_tools(tmp_path, resolved)
    json.dumps(tools["calculate"].openai_schema)
    assert not approvals
    assert await tools["calculate"].call({"budget": 150000, "actual": 183000}) == 22
    assert not list(tmp_path.rglob("__pycache__"))


def test_locked_python_tool_tamper_and_permission(tmp_path):
    resolved, path = fixture_tool(tmp_path)
    resolved["security"]["allowedPermissions"] = []
    with pytest.raises(ValueError, match="host execution"):
        assemble_python_tools(tmp_path, resolved)
    resolved["security"]["allowedPermissions"] = ["process:host-user"]
    path.write_text("raise RuntimeError('tampered')")
    with pytest.raises(ValueError, match="digest mismatch"):
        assemble_python_tools(tmp_path, resolved)


@pytest.mark.asyncio
async def test_locked_python_tool_timeout_and_approval(tmp_path):
    resolved, _ = fixture_tool(tmp_path, "import time\ndef calculate(): time.sleep(30)")
    resolved["capabilities"]["tools"][0]["approval"] = "always"
    tools, approvals = assemble_python_tools(tmp_path, resolved)
    assert approvals == {"calculate"}
    with pytest.raises(TimeoutError):
        await tools["calculate"].call({})


@pytest.mark.asyncio
async def test_builtin_concurrent_identity_and_agent_workspaces_are_isolated(tmp_path):
    from ksadk.runtime_context import (
        platform_invocation_scope,
        session_invocation_context,
        tool_execution_scope,
    )

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    contracts = {"capabilities": {"tools": [builtin_contract(name) for name in (
        "write_workspace_file", "read_workspace_file", "edit_workspace_file",
    )]}}

    async def drive(agent, tenant, subject):
        marker = f"{agent}-{tenant}-{subject}"
        tools, _ = assemble_python_tools(bundle, contracts, workspace_root=tmp_path / agent)
        context = session_invocation_context({}, agent_id=agent, identity={
            "identity_namespace": "acceptance", "tenant_id": tenant,
            "subject_type": "user", "subject_id": subject,
        })
        with platform_invocation_scope(context), tool_execution_scope("same-session"):
            assert (await tools["write_workspace_file"].call({
                "path": "shared-name.txt", "content": marker,
            }))["ok"]
            result = await tools["read_workspace_file"].call({"path": "shared-name.txt"})
            assert marker in json.dumps(result)
            assert (await tools["edit_workspace_file"].call({
                "path": "shared-name.txt", "old_text": marker, "new_text": marker + "-ok",
            }))["ok"]
        return marker + "-ok"

    markers = await asyncio.gather(*(
        drive(*scope) for scope in (
            ("agent-a", "tenant-a", "user-a"), ("agent-a", "tenant-a", "user-b"),
            ("agent-a", "tenant-b", "user-a"), ("agent-b", "tenant-a", "user-a"),
        )
    ))
    assert sorted(p.read_text() for p in tmp_path.rglob("shared-name.txt")) == sorted(markers)
    assert not list(bundle.iterdir())


@pytest.mark.asyncio
async def test_cancel_python_tool_kills_child_before_late_side_effect(tmp_path):
    marker = tmp_path / "started"
    late = tmp_path / "must-not-exist"
    source = (
        "import pathlib, time\n"
        "def calculate():\n"
        f" pathlib.Path({str(marker)!r}).touch()\n"
        " time.sleep(2)\n"
        f" pathlib.Path({str(late)!r}).touch()\n"
    )
    resolved, _ = fixture_tool(tmp_path, source)
    resolved["capabilities"]["tools"][0]["timeoutSeconds"] = 10
    tools, _ = assemble_python_tools(tmp_path, resolved)
    task = asyncio.create_task(tools["calculate"].call({}))
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(2.1)
    assert not late.exists()


@pytest.mark.asyncio
async def test_supervisor_sigkill_stops_orphan_tool_before_late_write(tmp_path):
    import os
    import signal
    import sys
    from pathlib import Path

    marker, late = tmp_path / "child-pid", tmp_path / "late-write"
    source = (
        "import os, pathlib, time\n"
        "def calculate():\n"
        f" pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        " time.sleep(2)\n"
        f" pathlib.Path({str(late)!r}).touch()\n"
    )
    resolved, _ = fixture_tool(tmp_path, source)
    resolved["capabilities"]["tools"][0]["timeoutSeconds"] = 10
    supervisor = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import asyncio, json, sys; from pathlib import Path; "
        "from ksadk.plugins.providers.harness_tools import assemble_python_tools; "
        "tools, _ = assemble_python_tools(Path(sys.argv[1]), json.loads(sys.argv[2])); "
        "asyncio.run(tools['calculate'].call({}))",
        str(tmp_path), json.dumps(resolved),
        cwd=Path(__file__).resolve().parents[2],
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                await asyncio.sleep(0.01)
        supervisor.kill()
        await supervisor.wait()
        await asyncio.sleep(2.2)
        assert not late.exists(), "Tool continued writing after its supervisor was killed"
    finally:
        if supervisor.returncode is None:
            supervisor.kill()
        await supervisor.wait()
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
