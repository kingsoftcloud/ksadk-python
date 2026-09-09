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
