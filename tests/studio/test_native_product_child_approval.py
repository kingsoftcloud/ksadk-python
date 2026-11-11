"""Ordinary Studio routes child approvals without bypassing original file tools."""

import asyncio
import json

import pytest

from ksadk.harness.native.state import RunPhase
from ksadk.harness.spec import SubAgentBinding
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
from tests.studio.test_native_product_edit_revision import tool_result


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("decision", ["approve", "deny", "cancel"])
async def test_parent_card_resolves_real_child_file_approval(
    tmp_path, monkeypatch, durable, background, decision, *, user_input=False,
    role_timeout=120.0, approval_delay=0.0,
):
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("NATIVE_CHILD_APPROVAL_FIXTURE_KEY", "fixture-key")
    workspace = tmp_path / "studio/.harness-tools/workspace"
    workspace.mkdir(parents=True)
    target = workspace / "demo.py"
    target.write_bytes(b"alpha = 1\n")

    class Client:
        def __init__(self):
            self.parent_requests, self.child_requests = [], []

        async def complete(self, _model, **kwargs):
            parent = any(t["function"]["name"] == "native_delegate" for t in kwargs["tools"])
            requests = self.parent_requests if parent else self.child_requests
            step = len(requests)
            requests.append(kwargs)
            name, args, text = None, {}, ""
            if parent:
                if step == 0:
                    name, args = (
                        "native_delegate",
                        {
                            "role": "writer",
                            "task": "Change alpha from 1 to 2 in demo.py.",
                            "background": background,
                        },
                    )
                elif step == 1:
                    receipt = tool_result(kwargs["messages"], "parent-0")
                    assert receipt["status"] == "ok", receipt
                    self.child_id = receipt["content"]["child_run_id"]
                    name, args = (
                        "native_child_control",
                        {
                            "action": "wait",
                            "child_run_id": self.child_id,
                        },
                    )
                else:
                    assert step == 2
                    text = "Child result received. The isolated files have not been merged."
            elif step == 0:
                name, args = "read_workspace_file", {"path": "demo.py"}
            elif step == 1:
                assert tool_result(kwargs["messages"], "child-0")["status"] == "ok"
                name, args = (
                    "edit_workspace_file",
                    {
                        "path": "demo.py",
                        "old_text": "alpha = 1",
                        "new_text": "alpha = 2",
                    },
                )
            else:
                assert step == 2
                if user_input:
                    assert "Retain the original approval decision." in str(kwargs["messages"])
                receipt = tool_result(kwargs["messages"], "child-1")
                assert receipt["status"] == ("ok" if decision == "approve" else "error"), receipt
                text = "Updated isolated file." if decision == "approve" else "Write was denied."
            return ModelResponse(
                content=text,
                finish_reason="tool_calls" if name else "stop",
                raw_message={},
                tool_calls=[
                    ToolCall(
                        id=f"{'parent' if parent else 'child'}-{step}",
                        name=name,
                        arguments=json.dumps(args),
                    )
                ]
                if name
                else [],
                usage=Usage(input_tokens=40, output_tokens=8, total_tokens=48, reported=True),
            )

    client = Client()
    studio = StudioService(
        tmp_path / "studio",
        model_client=client,
        native_harness_agent_ids=("child-approval-agent",),
        native_harness_durable=durable,
    )
    task = None
    try:
        tools = {item.name: item for item in studio.catalog.list(kind="tool", limit=200)}
        names = ("read_workspace_file", "edit_workspace_file")
        studio.create_agent(
            agent_id="child-approval-agent",
            name="Child approval",
            spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Delegate and report."),
                model=ModelSpec(
                    model="fixture-model",
                    endpoint_url="https://example.invalid/v1/chat/completions",
                    credential_ref="env://NATIVE_CHILD_APPROVAL_FIXTURE_KEY",
                ),
                bindings=AgentBindings(
                    policy_template="loose",
                    tools=[
                        CapabilityBinding(resource_id=tools[name].resource_id) for name in names
                    ],
                ),
                security=SecuritySpec(
                    allowed_permissions=["process:host-user"],
                    network=NetworkPolicy(allowed_hosts=["example.invalid"]),
                ),
                sub_agents=(
                    SubAgentBinding(
                        name="writer",
                        tools=names,
                        max_turns=6,
                        timeout_seconds=role_timeout,
                        instructions="Edit only the isolated demo.py.",
                    ),
                ),
            ),
        )
        build = await studio.ensure_current_build("child-approval-agent")
        session = "child-approval-session"
        task = asyncio.create_task(
            studio.run_build(
                build.id,
                "Delegate the isolated file update and report without merging.",
                session,
                approval_mode="risk",
            )
        )
        async with asyncio.timeout(20):
            while True:
                runs = studio.event_store.list_runs(session_id=session)
                if runs and runs[0].status == RunStatus.WAITING_INPUT:
                    break
                if task.done():
                    raise AssertionError(
                        (
                            (await task).error,
                            len(client.parent_requests),
                            len(client.child_requests),
                        )
                    )
                await asyncio.sleep(0.01)
        record = runs[0]
        approval = next(
            e for e in studio.event_store.events(record.id) if e.type == "approval.requested"
        )
        detail = approval.data["detail"]
        assert detail["source"] == "child_tool"
        assert detail["tool_name"] == "edit_workspace_file"
        assert detail["arguments"] == {
            "path": "demo.py",
            "old_text": "alpha = 1",
            "new_text": "alpha = 2",
        }
        assert approval.data["callId"] == "child-1"
        from ksadk.harness.native.state import RunScope

        scope = RunScope.model_validate(detail["child_scope"])
        store = studio._native_product_composer.store
        child = store.load(scope, detail["child_run_id"])
        assert child.phase == RunPhase.AWAITING_INTERACTION
        files = list(
            (tmp_path / "studio/.agentkit/plugin-runtime/state/child-workspaces").glob(
                "*/work/demo.py"
            )
        )
        assert len(files) == 1
        assert files[0].parent.parent.name == child.origin["workspace_isolation_id"]
        assert files[0].read_bytes() == b"alpha = 1\n"
        # The isolation identity is opaque; obtain the actual file via the
        # read receipt produced by the child Worker rather than trusting it.
        read_receipt = next(m for m in child.messages if m.get("tool_call_id") == "child-0")
        assert "alpha = 1" in str(read_receipt)
        assert target.read_bytes() == b"alpha = 1\n"
        assert len(client.child_requests) == 2
        if approval_delay:
            await asyncio.sleep(approval_delay)
            waiting_child = store.load(scope, child.run_id)
            assert waiting_child.phase == RunPhase.AWAITING_INTERACTION
            assert waiting_child.pending_interaction == child.pending_interaction
            assert waiting_child.execution_elapsed_seconds == child.execution_elapsed_seconds
            assert studio.event_store.get(record.id).status == RunStatus.WAITING_INPUT
            assert len(client.child_requests) == 2 and not task.done()
        if user_input:
            import httpx

            from ksadk.studio.api import create_studio_app

            app = create_studio_app(tmp_path / "studio", service=studio, security_enabled=False)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as http:
                response = await http.get(f"/api/v1/runs/{record.id}/children")
                assert response.status_code == 200, response.text
                (row,) = response.json()["tasks"]
                response = await http.post(
                    f"/api/v1/runs/{record.id}/children/{child.run_id}:send",
                    json={
                        "command_id": "waiting-child-input",
                        "text": "Retain the original approval decision.",
                        "expected_goal_revision": row["goal_revision"],
                    },
                )
                assert response.status_code == 202, response.text
            parent_id = store.session_head(scope, session).run_id
            async with asyncio.timeout(5):
                while not any(
                    event.kind == "child.input_queued" for event in store.events(scope, parent_id)
                ):
                    await asyncio.sleep(0.01)
            waiting_child = store.load(scope, child.run_id)
            assert waiting_child.phase == RunPhase.AWAITING_INTERACTION
            assert waiting_child.pending_interaction == child.pending_interaction
            assert studio.event_store.get(record.id).status == RunStatus.WAITING_INPUT
            assert len(client.child_requests) == 2 and files[0].read_bytes() == b"alpha = 1\n"
        if decision == "cancel":
            await studio.run_service.cancel_run(record.id)
        else:
            await studio.run_service.submit_interaction(
                record.id,
                approval.data["approvalId"],
                name=decision,
                data={"decision": decision},
                expected_revision=1,
                idempotency_key="child-decision",
            )
        if decision == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 20)
            result = studio.event_store.get(record.id)
            assert result.status == RunStatus.CANCELLED
        else:
            result = await asyncio.wait_for(task, 20)
            assert result.status == RunStatus.COMPLETED, result.error
            assert result.verification_outcome == "unverified"
        child = store.load(scope, child.run_id)
        assert child.phase == (RunPhase.CANCELED if decision == "cancel" else RunPhase.COMPLETED)
        if user_input:
            assert child.pending_guidance_ids == ()
            assert store.commands(scope, child.run_id) == ()
        assert target.read_bytes() == b"alpha = 1\n"
        assert len(client.child_requests) == (2 if decision == "cancel" else 3)
        assert files[0].read_bytes() == (
            b"alpha = 2\n" if decision == "approve" else b"alpha = 1\n"
        )
        parent = store.session_head(scope, session)
        assert parent.descendant_model_calls == len(client.child_requests)
        assert parent.child_budget_reservations[0].settled_usage_digest is not None
        edited = [m for m in child.messages if m.get("tool_call_id") == "child-1"]
        assert len(edited) == (0 if decision == "cancel" else 1)
        if decision != "cancel":
            assert tool_result(child.messages, "child-1")["status"] == (
                "ok" if decision == "approve" else "error"
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await studio.aclose()
