"""Ordinary Studio edits preserve external changes across builtin worker processes."""

import json
import os
import sqlite3

import pytest

from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    CapabilityBinding,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RunStatus,
    RuntimeRef,
    SecuritySpec,
    Usage,
)
from ksadk.studio.model_client import ModelResponse, ToolCall
from ksadk.studio.service import StudioService
from tests.studio.test_dsh_provider_registration import _managed_profile
from tests.studio.test_native_workspace_diagnostics import call, surface


def tool_result(messages, call_id):
    message = next(m for m in messages
                   if m.get("role") == "tool" and m.get("tool_call_id") == call_id)
    content = message["content"]
    if isinstance(content, list):
        content = content[0]["text"]
    return json.loads(content)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["edit_workspace_file", "multi_edit_workspace_file"])
async def test_ordinary_studio_rejects_stale_edit_then_uses_fresh_observed_revision(
    tmp_path, monkeypatch, name
):
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("NATIVE_EDIT_REVISION_TEST_KEY", "fixture-key")
    root = tmp_path / "studio" / ".harness-tools" / "workspace"
    root.mkdir(parents=True)
    target = root / "demo.py"
    target.write_bytes(b"value = 1\nother = 'a'\n")
    external = b"value = 1\nother = 'b'\n"
    names = ("read_workspace_file", name, "read_workspace_file", name)
    changes = {"old_text": "value = 1", "new_text": "value = 2"}

    class Client:
        def __init__(self):
            self.requests = []

        async def complete(self, _model, **kwargs):
            step = len(self.requests)
            self.requests.append(kwargs)
            if step == 1:
                assert tool_result(kwargs["messages"], "step-0")["status"] == "ok"
                observed = target.stat()
                target.write_bytes(external)
                os.utime(target, ns=(observed.st_atime_ns, observed.st_mtime_ns))
            elif step == 2:
                stale = tool_result(kwargs["messages"], "step-1")
                assert stale["status"] == "error"
                assert stale["content"]["error_type"] == "file_modified_since_read"
                assert "read_workspace_file" in stale["content"]["suggested_action"]
                assert target.read_bytes() == external
            elif step == 3:
                fresh = tool_result(kwargs["messages"], "step-2")
                assert fresh["status"] == "ok"
                assert "other = 'b'" in fresh["content"]["content"]
            elif step == 4:
                assert tool_result(kwargs["messages"], "step-3")["status"] == "ok"
                assert target.read_bytes() == external.replace(b"value = 1", b"value = 2")
            arguments = {"path": "demo.py"}
            if step in {1, 3}:
                arguments.update({"edits": [changes]} if name.startswith("multi") else changes)
            return ModelResponse(
                content="" if step < len(names) else "Updated only the requested value.",
                finish_reason="tool_calls" if step < len(names) else "stop",
                usage=Usage(input_tokens=40, output_tokens=8, total_tokens=48, reported=True),
                raw_message={}, tool_calls=[ToolCall(id=f"step-{step}", name=names[step],
                    arguments=json.dumps(arguments))] if step < len(names) else [],
            )

    client = Client()
    studio = StudioService(
        tmp_path / "studio", model_client=client, native_harness_agent_ids=("revision-agent",)
    )
    try:
        catalog = {item.name: item for item in studio.catalog.list(kind="tool", limit=200)}
        studio.create_agent(
            agent_id="revision-agent", name="Revision check",
            spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Inspect the file before editing."),
                model=ModelSpec(
                    model="fixture-model", endpoint_url="https://example.invalid/v1/chat/completions",
                    credential_ref="env://NATIVE_EDIT_REVISION_TEST_KEY",
                ),
                bindings=AgentBindings(policy_template="loose", tools=[
                    CapabilityBinding(resource_id=catalog[tool].resource_id)
                    for tool in {"read_workspace_file", name}
                ]),
                security=SecuritySpec(allowed_permissions=["process:host-user"],
                    network=NetworkPolicy(allowed_hosts=["example.invalid"])),
            ),
        )
        build = await studio.ensure_current_build("revision-agent")
        run = await studio.run_build(
            build.id, "Inspect demo.py and change value from 1 to 2, preserving all other content.",
            "revision-session", approval_mode="full",
        )
        assert run.status == RunStatus.COMPLETED, run.error
        assert run.runtime_handle["native_ref"]["engine_kind"] == "native"
        assert len(client.requests) == 5
        assert target.read_bytes() == external.replace(b"value = 1", b"value = 2")
        assert run.verification_outcome == "unverified"
    finally:
        await studio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["edit_workspace_file", "multi_edit_workspace_file"])
async def test_builtin_worker_legacy_read_rows_upgrade_only_after_fresh_read(tmp_path, name):
    tools, _ = surface(tmp_path, ("read_workspace_file", name))
    root = tmp_path / "studio" / ".harness-tools" / "workspace"
    root.mkdir(parents=True)
    target = root / "demo.py"
    original = b"value = 1\n"
    target.write_bytes(original)
    assert (await call(tools, "read_workspace_file", {"path": "demo.py"})).status == "ok"
    databases = list((tmp_path / "studio" / ".harness-tools" / "state").glob("*.sqlite"))
    assert len(databases) == 1
    with sqlite3.connect(databases[0]) as db:
        raw = db.execute("SELECT data FROM reads WHERE session=?", ("s",)).fetchone()[0]
        rows = json.loads(raw)
        for row in rows:
            row.pop("canonical_path", None)
            row.pop("content_sha256", None)
        db.execute("UPDATE reads SET data=? WHERE session=?", (json.dumps(rows), "s"))
    changes = {"old_text": "value = 1", "new_text": "value = 2"}
    args = {"path": "demo.py", **({"edits": [changes]} if name.startswith("multi") else changes)}
    denied = await call(tools, name, args)
    assert denied.status == "error" and denied.content["error_type"] == "file_not_read"
    assert target.read_bytes() == original
    assert (await call(tools, "read_workspace_file", {"path": "demo.py"})).status == "ok"
    assert (await call(tools, name, args)).status == "ok"
    assert target.read_bytes() == b"value = 2\n"
