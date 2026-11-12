"""An owned child deadline must retain its original Run and consumed model call."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ksadk.harness.native.state import RunScope
from ksadk.harness.spec import SubAgentBinding
from ksadk.studio.contracts import (
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
    Usage,
)
from ksadk.studio.model_client import ModelResponse, ToolCall
from ksadk.studio.native_subagent_port import StudioNativeSubagentPort
from ksadk.studio.service import StudioService
from tests.studio.test_dsh_provider_registration import _managed_profile


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_foreground_deadline_retains_owned_child_usage(
    tmp_path,
    monkeypatch,
    durable,
    *,
    slow_assembly=False,
):
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("CHILD_DEADLINE_FIXTURE_KEY", "fixture-key")
    if slow_assembly:
        from ksadk.studio import native_subagent_port

        original = native_subagent_port._StudioChildPort.__init__

        def bind(self, **kwargs):
            factory = kwargs["factory"]

            async def prepare(child):
                # Host setup belongs to the parent effect lease, before this
                # child's active clock exists. Do not alter Kernel time.
                await asyncio.sleep(0.7)
                return await factory(child)

            original(self, **{**kwargs, "factory": prepare})

        monkeypatch.setattr(native_subagent_port._StudioChildPort, "__init__", bind)

    class Client:
        def __init__(self):
            self.parent, self.child = [], []
            self.child_cancelled = asyncio.Event()

        async def complete(self, _model, **kwargs):
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            if "native_delegate" not in names:
                self.child.append(kwargs)
                if slow_assembly:
                    return ModelResponse(
                        content="Child findings.",
                        finish_reason="stop",
                        raw_message={},
                        tool_calls=[],
                        usage=Usage(
                            input_tokens=20, output_tokens=5, total_tokens=25, reported=True
                        ),
                    )
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.child_cancelled.set()
                    raise
            self.parent.append(kwargs)
            if len(self.parent) == 1:
                return ModelResponse(
                    content="",
                    finish_reason="tool_calls",
                    raw_message={},
                    tool_calls=[
                        ToolCall(
                            id="deadline-child",
                            name="native_delegate",
                            arguments=json.dumps({"role": "reviewer", "task": "Inspect the task."}),
                        )
                    ],
                    usage=Usage(input_tokens=20, output_tokens=5, total_tokens=25, reported=True),
                )
            return ModelResponse(
                content="The child did not deliver a verified result.",
                finish_reason="stop",
                raw_message={},
                tool_calls=[],
                usage=Usage(input_tokens=20, output_tokens=5, total_tokens=25, reported=True),
            )

    client = Client()
    studio = StudioService(
        tmp_path / "studio",
        model_client=client,
        native_harness_agent_ids=("deadline-agent",),
        native_harness_durable=durable,
    )
    try:
        studio.create_agent(
            agent_id="deadline-agent",
            name="Child deadline",
            spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Delegate the task to the reviewer."),
                model=ModelSpec(
                    model="fixture-model",
                    credential_ref="env://CHILD_DEADLINE_FIXTURE_KEY",
                    endpoint_url="https://example.invalid/v1/chat/completions",
                ),
                security=SecuritySpec(
                    allowed_permissions=["process:host-user"],
                    network=NetworkPolicy(allowed_hosts=["example.invalid"]),
                ),
                sub_agents=(
                    SubAgentBinding(
                        name="reviewer", instructions="Inspect the task.", timeout_seconds=0.5
                    ),
                ),
            ),
        )
        build = await studio.ensure_current_build("deadline-agent")
        run = await asyncio.wait_for(
            studio.run_build(build.id, "Delegate the inspection.", "deadline-session"), timeout=15
        )
        assert len(client.child) == 1, run.error
        assert client.child_cancelled.is_set() is not slow_assembly
        metadata = await studio.session_service.get_session_metadata("deadline-session")
        scope = RunScope(
            tenant_id="local-studio",
            workspace_id=studio.workspace_record.workspace_id,
            user_id=metadata.user_id,
            agent_id=metadata.agent_id,
        )
        store = studio._native_product_composer.store
        parent = store.session_head(scope, "deadline-session")
        assert len(parent.child_run_ids) == 1, (
            parent.phase,
            parent.outcome,
            parent.child_run_ids,
            parent.descendant_model_calls,
        )
        child = store.load(scope, parent.child_run_ids[0])
        assert child.phase.terminal and child.model_calls == 1
        assert parent.descendant_model_calls == child.model_calls
        assert child.origin["parent_run_id"] == parent.run_id
        assert (
            any(e.kind == "usage.unavailable" for e in store.events(scope, child.run_id))
            is not slow_assembly
        )
        prepared = next(
            e
            for e in store.events(scope, parent.run_id)
            if e.kind == "effect.prepared" and e.payload.get("kind") == "tool"
        )
        receipt = store.get_receipt(scope, parent.run_id, prepared.payload["operation_key"])
        assert receipt.status == "committed"
        assert receipt.result["content"]["child_run_id"] == child.run_id
        assert receipt.result["content"]["resource_usage_unknown"] is not slow_assembly
        assert receipt.result["resource_usage"]["model_calls"] == 1
        assert receipt.result["resource_usage"]["input_tokens"] == (20 if slow_assembly else None)
        assert receipt.result["resource_usage"]["output_tokens"] == (5 if slow_assembly else None)
        if slow_assembly:
            assert child.phase.value == "completed" and child.outcome == "unverified"
            assert len(client.parent) == 2
            assert child.execution_elapsed_seconds < 0.5
        assert run.verification_outcome != "verified"
    finally:
        await studio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_parent_effect_owns_slow_host_assembly(tmp_path, monkeypatch, durable):
    await test_foreground_deadline_retains_owned_child_usage(
        tmp_path,
        monkeypatch,
        durable,
        slow_assembly=True,
    )


@pytest.mark.asyncio
@pytest.mark.local_process_heavy
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("background", [False, True])
async def test_approval_wait_does_not_consume_child_execution_budget(
    tmp_path,
    monkeypatch,
    durable,
    background,
):
    from tests.studio import test_native_product_child_approval as approval_fixture
    from tests.studio.test_native_product_child_approval import (
        test_parent_card_resolves_real_child_file_approval,
    )

    now, waits = [0.0], []
    monkeypatch.setattr("ksadk.harness.native.run_clock.monotonic", lambda: now[0])

    class ApprovalWaitClock:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, seconds):
            if seconds == 3.5:
                # The shared fixture has already observed AWAITING_INTERACTION.
                # Advance beyond the child's unchanged 3s budget there, while
                # excluding unrelated host/GC time from active execution.
                waits.append(seconds)
                now[0] += seconds
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(seconds)

    monkeypatch.setattr(approval_fixture, "asyncio", ApprovalWaitClock())
    await test_parent_card_resolves_real_child_file_approval(
        tmp_path,
        monkeypatch,
        durable,
        background,
        "approve",
        role_timeout=3.0,
        approval_delay=3.5,
    )
    assert waits == [3.5]


@pytest.mark.asyncio
async def test_unbound_custom_child_port_retains_legacy_timeout():
    class Port:
        async def execute(self, request):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    delegate = Port()
    port = StudioNativeSubagentPort(
        child_port=delegate,
        bindings={
            "reviewer": SubAgentBinding(
                name="reviewer",
                instructions="Review.",
                timeout_seconds=0.02,
            )
        },
    )
    result = await port.execute(
        SimpleNamespace(call=SimpleNamespace(arguments={"role": "reviewer"}))
    )
    assert delegate.cancelled
    assert result.status == "uncertain"
    assert result.content["error_code"] == "child_timeout_outcome_unknown"
    assert result.resource_usage.model_calls == result.resource_usage.tool_calls == 0
    assert result.resource_usage.child_run_ids == ()
    assert result.resource_usage.input_tokens is None
