import hashlib
import json
from types import MappingProxyType

import pytest

from ksadk.plugins.providers.harness_tools import assemble_python_tools, python_tool_bundle_path


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
