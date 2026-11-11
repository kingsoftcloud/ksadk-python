"""Post-write diagnostics are repair observations, never acceptance authority."""

from __future__ import annotations

import hashlib
import json

import pytest

from ksadk.harness.execution_policy import ExecutionPolicy, execution_policy_scope
from ksadk.harness.native.model import ModelTextContent, ModelToolCall
from ksadk.harness.native.ports import ToolRequest, digest
from ksadk.harness.native.state import RunScope, RunSnapshot
from ksadk.harness.stores.native_memory import MemoryRunStore
from ksadk.studio.native_tool_port import build_native_locked_tool_surface
from ksadk.toolsets import describe_agentengine_tools

SCOPE = RunScope(tenant_id="a", workspace_id="w", user_id="u", agent_id="agent")


def surface(tmp_path, names=("write_workspace_file",), authorize=None):
    store = MemoryRunStore()
    store.create(
        RunSnapshot(
            scope=SCOPE,
            run_id="run",
            session_id="s",
            turn_id="t",
            goal="Repair files",
        )
    )

    async def allow(_request):
        return "allow"

    contracts = {
        d["name"]: {
            "name": d["name"],
            "executor": "builtin",
            "inputSchema": d["input_schema"],
            "description": d["description"],
            "approval": "always",
            "sideEffect": "write",
        }
        for d in describe_agentengine_tools(profile="coding", mode="direct")
    }
    tools = build_native_locked_tool_surface(
        bundle_root=tmp_path,
        resolved_agent_spec={
            "agentId": "agent",
            "capabilities": {"tools": [contracts[n] for n in names]},
        },
        workspace_root=tmp_path / "studio",
        scope=SCOPE,
        store=store,
        authorize=authorize or allow,
    )
    return tools, store


async def call(tools, name, arguments, *, definition=None):
    definition = definition or next(d for d in tools.definitions if d.name == name)
    return await tools.executor.execute(
        ToolRequest(
            scope=SCOPE,
            run_id="run",
            operation_key="operation",
            call=ModelToolCall(call_id="call", name=name, arguments=arguments),
            definition=definition,
        )
    )


def diagnostics(result):
    blocks = [b for b in result.content_blocks if isinstance(b, ModelTextContent)]
    assert len(blocks) == 1
    value = json.loads(blocks[0].text)
    assert value["version"] == "workspace-diagnostics/v1"
    assert value["scope"] == "python_parse_only"
    assert value["acceptance"] == "not_evaluated"
    return value


@pytest.mark.asyncio
async def test_real_builtin_write_returns_diagnostic_for_actual_bytes_then_clears_it(tmp_path):
    tools, _ = surface(tmp_path)
    root = tmp_path / "studio/.harness-tools/workspace"
    invalid = "def compute(:\n    return 42\n"
    broken = await call(tools, "write_workspace_file", {"path": "src/calc.py", "content": invalid})
    assert broken.status == "ok"  # The write itself really succeeded.
    assert (root / "src/calc.py").read_text() == invalid
    report = diagnostics(broken)
    item = report["files"][0]
    assert item["status"] == "error"
    assert item["code"] == "python_syntax_error"
    assert item["line"] == 1 and item["column"] > 0
    assert item["path"] == "src/calc.py"
    assert item["content_digest"] == "sha256:" + hashlib.sha256(invalid.encode()).hexdigest()
    correct = "def compute():\n    return 42\n"
    fixed = await call(tools, "write_workspace_file", {"path": "src/calc.py", "content": correct})
    assert diagnostics(fixed)["files"][0]["status"] == "passed"
    assert diagnostics(fixed)["files"][0]["content_digest"] != item["content_digest"]
    assert "diagnostics" not in broken.content


@pytest.mark.asyncio
async def test_diagnostic_reader_uses_active_policy_root_and_does_not_execute_code(tmp_path):
    tools, _ = surface(tmp_path)
    root = tmp_path / "policy-workspace"
    sentinel = tmp_path / "must-not-exist"
    content = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n"
    with execution_policy_scope(ExecutionPolicy(workspace_root=root)):
        result = await call(tools, "write_workspace_file", {"path": "calc.py", "content": content})
    assert result.status == "ok"
    assert diagnostics(result)["files"][0]["status"] == "passed"
    assert (root / "calc.py").read_text() == content
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_unsupported_file_is_not_a_successful_diagnostic(tmp_path):
    tools, _ = surface(tmp_path)
    result = await call(tools, "write_workspace_file", {"path": "calc.js", "content": "broken ???"})
    item = diagnostics(result)["files"][0]
    assert result.status == "ok"
    assert item["status"] == "not_run"
    assert item["code"] == "unsupported_file_type"


@pytest.mark.asyncio
async def test_diagnostic_profile_is_locked_before_write_authorization(tmp_path):
    authorized = []

    async def allow(request):
        authorized.append(request)
        return "allow"

    tools, _ = surface(tmp_path, authorize=allow)
    original = tools.definitions[0]
    assert original.feedback_profile_digest.startswith("sha256:")
    assert "feedback_profile_digest" not in original.function_schema()
    legacy = original.model_copy(update={"feedback_profile_digest": None})
    assert digest(legacy) != digest(original)
    result = await call(
        tools,
        original.name,
        {"path": "calc.py", "content": "x=1"},
        definition=legacy,
    )
    assert result.status == "error" and result.content == {"code": "definition_mismatch"}
    assert authorized == []


@pytest.mark.parametrize("name", ["edit_workspace_file", "multi_edit_workspace_file"])
@pytest.mark.asyncio
async def test_unobserved_file_requires_read_and_successful_edits_get_diagnostics(
    tmp_path, name
):
    tools, _ = surface(tmp_path, ("read_workspace_file", name))
    root = tmp_path / "studio/.harness-tools/workspace"
    root.mkdir(parents=True)
    # An external file is unobserved. A successful write by this Session is
    # covered separately as an observed revision and must not require reread.
    (root / "calc.py").write_bytes(b"answer = 42\n")
    changes = {"old_text": "answer = 42", "new_text": "answer = ("}
    args = {"path": "calc.py", **({"edits": [changes]} if name.startswith("multi") else changes)}
    unread = await call(tools, name, args)
    assert unread.status == "error"
    assert unread.content_blocks == ()
    await call(tools, "read_workspace_file", {"path": "calc.py"})
    edited = await call(tools, name, args)
    assert edited.status == "ok"
    assert diagnostics(edited)["files"][0]["status"] == "error"
    assert (root / "calc.py").read_text() == "answer = (\n"


@pytest.mark.asyncio
async def test_multiple_write_reports_each_observed_file(tmp_path):
    tools, _ = surface(tmp_path, ("write_workspace_files",))
    result = await call(
        tools,
        "write_workspace_files",
        {
            "files": [
                {"path": "good.py", "content": "answer=42"},
                {"path": "bad.py", "content": "answer=("},
                {"path": "other.ts", "content": "???"},
            ]
        },
    )
    report = diagnostics(result)
    assert [(d["path"], d["status"]) for d in report["files"]] == [
        ("good.py", "passed"),
        ("bad.py", "error"),
        ("other.ts", "not_run"),
    ]
    assert report["omitted_file_count"] == 0
    assert len(result.content["written"]) == 3


@pytest.mark.asyncio
async def test_denied_write_never_reads_or_mutates_workspace(tmp_path, monkeypatch):
    from ksadk.studio import native_tool_port

    def never(**_args):
        raise AssertionError("diagnostic read before authorization")

    async def deny(_request):
        return "deny"

    monkeypatch.setattr(native_tool_port, "observe_workspace_writes", never)
    tools, _ = surface(tmp_path, authorize=deny)
    result = await call(tools, "write_workspace_file", {"path": "calc.py", "content": "x=1"})
    assert result.content == {"code": "policy_denied"}
    assert result.content_blocks == ()
    assert not (tmp_path / "studio/.harness-tools/workspace/calc.py").exists()


@pytest.mark.parametrize("path", ["../private.py", "/private.py", "sub/link.py", "dir/file.py"])
def test_diagnostic_reader_cannot_follow_escape_paths_or_symlinks(tmp_path, path):
    from ksadk.studio.native_workspace_diagnostics import observe_workspace_writes

    root = tmp_path / "workspace"
    root.mkdir()
    secret = tmp_path / "private.py"
    secret.write_text("hidden='do-not-disclose'\ndef bad(:")
    (root / "sub").mkdir()
    (root / "sub/link.py").symlink_to(secret)
    (root / "dir").symlink_to(tmp_path, target_is_directory=True)
    block = observe_workspace_writes(root=root, paths=(path,))
    item = json.loads(block.text)["files"][0]
    assert item["status"] == "not_run"
    assert "content_digest" not in item
    assert "do-not-disclose" not in block.text


def test_syntax_error_feedback_does_not_return_source_or_arbitrary_parser_message(tmp_path):
    from ksadk.studio.native_workspace_diagnostics import observe_workspace_writes

    (tmp_path / "calc.py").write_text("secret = 'do-not-disclose\n")
    result = observe_workspace_writes(root=tmp_path, paths=("calc.py",))
    assert "do-not-disclose" not in result.text
    assert "unterminated" not in result.text
    assert json.loads(result.text)["files"][0]["code"] == "python_syntax_error"


def test_diagnostics_bound_file_count_bytes_and_omitted_status(tmp_path):
    from ksadk.studio.native_workspace_diagnostics import (
        MAX_FILE_BYTES,
        MAX_FILES,
        observe_workspace_writes,
    )

    (tmp_path / "large.py").write_bytes(b"#" + b"x" * MAX_FILE_BYTES)
    paths = ("large.py", *(f"missing-{i}.py" for i in range(20)))
    report = json.loads(observe_workspace_writes(root=tmp_path, paths=paths).text)
    assert len(report["files"]) == MAX_FILES
    assert report["omitted_file_count"] == len(paths) - MAX_FILES
    assert report["files"][0]["status"] == "not_run"
    assert report["files"][0]["code"] == "byte_budget_exceeded"


def test_absent_feedback_profile_preserves_legacy_serialization_and_digest():
    from ksadk.harness.native.ports import ToolDefinition
    from tests.harness.native.test_kernel import config

    definition = ToolDefinition(name="tool", description="d", parameters={})
    payload = definition.model_dump(mode="json")
    assert "feedback_profile_digest" not in payload
    legacy_payload = dict(payload)
    legacy_payload.pop("progress_observation")
    legacy_payload.pop("context_role")
    expected = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest(definition) == expected
    configured = config().model_copy(update={"tools": (definition,)})
    assert "feedback_profile_digest" not in configured.model_dump(mode="json")["tools"][0]


@pytest.mark.asyncio
async def test_kernel_delivers_diagnostic_before_next_decision_and_keeps_acceptance_separate(
    tmp_path,
):
    from ksadk.harness.native.engine import NativeKernel
    from tests.harness.native.test_kernel import ScriptedModel, config

    tools, store = surface(tmp_path)
    model = ScriptedModel(
        {
            "tool_calls": [
                {
                    "call_id": "bad",
                    "name": "write_workspace_file",
                    "arguments": {"path": "calc.py", "content": "answer = ("},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "call_id": "fix",
                    "name": "write_workspace_file",
                    "arguments": {"path": "calc.py", "content": "answer = 42"},
                }
            ]
        },
        {"text": "Delivered."},
    )
    cfg = config().model_copy(update={"tools": tools.definitions})
    kernel = NativeKernel(
        store=store, scope=SCOPE, configuration=cfg, model=model, tools=tools.executor
    )
    run = kernel.start(goal="Write a Python module", session_id="kernel")
    result = await kernel.run_until_idle(run.run_id)
    assert len(model.requests) == 3
    for request, expected, call_id in (
        (model.requests[1], "error", "bad"),
        (model.requests[2], "passed", "fix"),
    ):
        message = next(m for m in request.messages if m.get("tool_call_id") == call_id)
        assert isinstance(message["content"], list)
        feedback = json.loads(message["content"][1]["text"])
        assert feedback["files"][0]["status"] == expected
        assert message["receipt_ref"]
        receipt = store.get_receipt(SCOPE, run.run_id, message["receipt_ref"])
        assert receipt.result["status"] == "ok"
        assert receipt.result["content_blocks"][0]["text"] == message["content"][1]["text"]
    assert result.outcome == "unverified"
    assert result.verification is None
    assert result.model_calls == 3 and result.tool_calls == 2


@pytest.mark.asyncio
async def test_custom_tool_with_same_name_has_no_builtin_diagnostic_authority(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from ksadk.studio import native_tool_port

    async def write(_arguments, **_kwargs):
        return {"ok": True, "path": "calc.py"}

    custom = SimpleNamespace(
        name="write_workspace_file",
        source="locked-python",
        description="custom",
        parameters={"type": "object"},
        call=write,
    )
    monkeypatch.setattr(
        native_tool_port, "assemble_python_tools", lambda *_a, **_k: ({custom.name: custom}, set())
    )
    tools, _ = surface(tmp_path)
    assert tools.definitions[0].feedback_profile_digest is None
    result = await call(tools, custom.name, {"path": "calc.py"})
    assert result.status == "ok" and result.content_blocks == ()


def test_nonregular_file_cannot_block_diagnostic_reader(tmp_path):
    import os

    from ksadk.studio.native_workspace_diagnostics import observe_workspace_writes

    os.mkfifo(tmp_path / "pipe.py")
    report = json.loads(observe_workspace_writes(root=tmp_path, paths=("pipe.py",)).text)
    assert report["files"][0]["status"] == "not_run"
    assert report["files"][0]["code"] == "file_unavailable"
