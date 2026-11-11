"""Cold starts through ordinary Studio/DSH preserve the opted-in Native ledger."""

from __future__ import annotations

import asyncio
import json

import pytest

from ksadk.harness.native.state import RunScope
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    CapabilityBinding,
    Instructions,
    ModelSpec,
    NativeRunOptions,
    NetworkPolicy,
    RunStatus,
    RuntimeRef,
    SecuritySpec,
    Usage,
)
from ksadk.studio.model_client import ModelResponse, ToolCall
from ksadk.studio.service import StudioService
from tests.studio.test_dsh_provider_registration import _managed_profile


class RecordingClient:
    def __init__(self, answer: str):
        self.answer = answer
        self.requests: list[dict] = []

    async def complete(self, _model, **kwargs):
        self.requests.append(kwargs)
        return ModelResponse(
            content=self.answer,
            finish_reason="stop",
            raw_message={},
            tool_calls=[],
            usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30, reported=True),
        )


def _create_agent(studio, *, tools=()):
    studio.create_agent(
        agent_id="durable-agent",
        name="Native durable product",
        spec=AgentSpec(
            runtime=RuntimeRef(type="harness"),
            instructions=Instructions(system="Answer the current user request."),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://example.invalid/v1/chat/completions",
                credential_ref="env://NATIVE_DURABILITY_TEST_KEY",
            ),
            bindings=AgentBindings(policy_template="loose", tools=list(tools)),
            security=SecuritySpec(
                allowed_permissions=["process:host-user"],
                network=NetworkPolicy(allowed_hosts=["example.invalid"]),
            ),
        ),
    )


def _scope(studio):
    return RunScope(
        tenant_id="local-studio",
        workspace_id=studio.workspace_record.workspace_id,
        user_id="local-user",
        agent_id="durable-agent",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_durability", [False, True])
async def test_ordinary_studio_native_completed_turn_survives_cold_start(
    tmp_path, monkeypatch, explicit_durability,
):
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("NATIVE_DURABILITY_TEST_KEY", "fixture-key")
    for name in ("KSADK_STUDIO_NATIVE_DEFAULT", "KSADK_STUDIO_NATIVE_AGENT_IDS",
                 "KSADK_STUDIO_NATIVE_DURABLE"):
        monkeypatch.delenv(name, raising=False)
    selection = {}
    if explicit_durability:
        monkeypatch.setenv("KSADK_STUDIO_NATIVE_DURABLE", "1")
        selection["native_harness_agent_ids"] = ("durable-agent",)
    root = tmp_path / "studio"
    first_client = RecordingClient("The sealed value is cobalt-48291.")
    first = StudioService(root, model_client=first_client, **selection)
    try:
        _create_agent(first)
        build = await first.ensure_current_build("durable-agent")
        original = await first.run_build(build.id, "Remember the sealed value.", "durable-session")
        assert original.status == RunStatus.COMPLETED, original.error
        original_native_id = original.runtime_handle["run_id"]
        scope = _scope(first)
        head = first._native_product_composer.store.session_head(scope, "durable-session")
        assert head.run_id == original_native_id
    finally:
        await first.aclose()

    second_client = RecordingClient("It is cobalt-48291.")
    second = StudioService(root, model_client=second_client, **selection)
    try:
        head = second._native_product_composer.store.session_head(scope, "durable-session")
        assert head is not None, "ordinary Studio discarded its committed Native session head"
        assert head.run_id == original_native_id
        assert head.phase.value == "completed"
        result = await second.run_build(build.id, "What is the sealed value?", "durable-session")
        assert result.status == RunStatus.COMPLETED, result.error
        projected = json.dumps(second_client.requests[0]["messages"], ensure_ascii=False)
        assert "cobalt-48291" in projected
        current = second._native_product_composer.store.session_head(scope, "durable-session")
        assert current.parent_run_id == original_native_id
        await second.run_build(build.id, "New unrelated conversation.", "separate-session")
        assert "cobalt-48291" not in json.dumps(second_client.requests[-1]["messages"])
    finally:
        await second.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary_default", [False, True])
async def test_explicit_false_preserves_memory_default_despite_environment(
    tmp_path, monkeypatch, ordinary_default,
):
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("KSADK_STUDIO_NATIVE_DURABLE", "1")
    selection = {} if ordinary_default else {"native_harness_agent_ids": ("durable-agent",)}
    studio = StudioService(tmp_path / "studio", native_harness_durable=False, **selection)
    try:
        from ksadk.harness.stores.native_memory import MemoryRunStore

        assert isinstance(studio._native_product_composer.store, MemoryRunStore)
        assert not list((tmp_path / "studio").rglob("native-harness.sqlite"))
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_native_default_environment_can_explicitly_select_memory(tmp_path, monkeypatch):
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.delenv("KSADK_STUDIO_NATIVE_AGENT_IDS", raising=False)
    monkeypatch.delenv("KSADK_STUDIO_NATIVE_DEFAULT", raising=False)
    monkeypatch.setenv("KSADK_STUDIO_NATIVE_DURABLE", "0")
    studio = StudioService(tmp_path / "studio")
    try:
        from ksadk.harness.stores.native_memory import MemoryRunStore

        assert studio._native_harness_default
        assert isinstance(studio._native_product_composer.store, MemoryRunStore)
    finally:
        await studio.aclose()


def test_durable_storage_requires_native_execution_when_default_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("KSADK_STUDIO_NATIVE_AGENT_IDS", raising=False)
    monkeypatch.setenv("KSADK_STUDIO_NATIVE_DURABLE", "1")
    with pytest.raises(ValueError, match="durable.*selected Studio agents"):
        StudioService(tmp_path / "studio", native_harness_default=False)


def test_invalid_durable_flag_fails_before_opening_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_STUDIO_NATIVE_DURABLE", "maybe")
    with pytest.raises(ValueError, match="KSADK_STUDIO_NATIVE_DURABLE"):
        StudioService(tmp_path / "studio", native_harness_agent_ids=("durable-agent",))
    assert not (tmp_path / "studio").exists()


async def _wait_for(predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.01)


async def _suspend_product(tmp_path, monkeypatch, projected_status=RunStatus.WAITING_INPUT):
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("NATIVE_DURABILITY_TEST_KEY", "fixture-key")
    monkeypatch.setenv("KSADK_STUDIO_NATIVE_DURABLE", "1")
    root = tmp_path / "studio"

    class WriteClient(RecordingClient):
        async def complete(self, _model, **kwargs):
            self.requests.append(kwargs)
            assert len(self.requests) == 1, "model advanced before tool approval"
            return ModelResponse(
                content="",
                finish_reason="tool_calls",
                raw_message={},
                usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30, reported=True),
                tool_calls=[ToolCall(
                    id="locked-write",
                    name="write_workspace_file",
                    arguments=json.dumps({"path": "approved.txt", "content": "written once"}),
                )],
            )

    first = StudioService(
        root, model_client=WriteClient("unused"), native_harness_agent_ids=("durable-agent",)
    )
    task = None
    try:
        write = next(item for item in first.catalog.list(kind="tool", limit=200)
                     if item.name == "write_workspace_file")
        _create_agent(first, tools=(CapabilityBinding(resource_id=write.resource_id),))
        build = await first.ensure_current_build("durable-agent")
        task = asyncio.create_task(first.run_build(
            build.id, "Write approved.txt after approval.", "approval-session"
        ))
        await _wait_for(lambda: first.event_store.list_runs()
                        and first.event_store.list_runs()[0].status == RunStatus.WAITING_INPUT)
        original = first.event_store.list_runs()[0]
        await _wait_for(lambda: first.run_service._waiting_modes.get(original.id) == "resume")
        assert not list(root.rglob("approved.txt"))
        approval = next(event for event in first.event_store.events(original.id)
                        if event.type == "approval.requested")
        scope = _scope(first)
        first.run_service._detaching_recoveries = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        first.run_service._detaching_recoveries = False
        # A projection can lag the canonical ledger when the process exits
        # after committing the suspension, before projecting WAITING_INPUT.
        original = first.event_store.get(original.id)
        original.status = projected_status
        first.event_store.save(original)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await first.aclose()
    return root, original, scope, approval


@pytest.mark.asyncio
@pytest.mark.parametrize("projected_status", [RunStatus.WAITING_INPUT, RunStatus.RUNNING])
@pytest.mark.parametrize("decision", ["approve", "reject", "cancel"])
async def test_ordinary_studio_cold_restore_keeps_approval_and_executes_write_once(
    tmp_path, monkeypatch, projected_status, decision
):
    root, original, scope, approval = await _suspend_product(
        tmp_path, monkeypatch, projected_status
    )
    original_handle = original.runtime_handle
    client = RecordingClient("The approved write is complete.")
    second = StudioService(root, model_client=client, native_harness_agent_ids=("durable-agent",))
    try:
        await second.start()
        await second.run_service.recover_interrupted(second.resolve_run_spec)
        await _wait_for(lambda: (
            second.run_service._waiting_modes.get(original.id) == "resume"
            and second.event_store.get(original.id).status == RunStatus.WAITING_INPUT
        ) or second.event_store.get(original.id).status == RunStatus.INTERRUPTED)
        restored = second.event_store.get(original.id)
        assert restored.status == RunStatus.WAITING_INPUT, restored.error
        assert restored.runtime_handle == original_handle
        assert client.requests == []
        assert not list(root.rglob("approved.txt"))
        await second.run_service.submit_interaction(
            original.id, approval.data["approvalId"], name=decision,
            data={"decision": decision}, expected_revision=1,
            idempotency_key="durable-product-approval",
        )
        await _wait_for(lambda: second.event_store.get(original.id).status in {
            RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED, RunStatus.CANCELLED,
        })
        await _wait_for(lambda: not second.run_service._recovery_tasks)
        finished = second.event_store.get(original.id)
        # Cancelling the approval withdraws this tool's grant; stopping the
        # entire Run is a separate control, matching the existing host contract.
        assert finished.status == RunStatus.COMPLETED, finished.error
        assert finished.runtime_handle == original_handle
        paths = list(root.rglob("approved.txt"))
        assert len(paths) == (1 if decision == "approve" else 0)
        if paths:
            assert paths[0].read_text() == "written once"
        events = second._native_product_composer.store.events(scope, original_handle["run_id"])
        receipts = []
        for event in events:
            if event.kind != "effect.prepared":
                continue
            key = event.payload["operation_key"]
            intent = second._native_product_composer.store.get_intent(
                scope, original_handle["run_id"], key
            )
            receipt = second._native_product_composer.store.get_receipt(
                scope, original_handle["run_id"], key
            )
            if intent.kind == "tool" and receipt is not None:
                receipts.append(receipt)
        assert sum(receipt.result.get("status") == "ok" for receipt in receipts) == (
            1 if decision == "approve" else 0
        )
        studio_events = second.event_store.events(original.id)
        assert sum(event.type == "approval.requested" for event in studio_events) == 1
        assert sum(event.type == "approval.resolved" for event in studio_events) == 1
        assert sum(event.type == "run.completed" for event in studio_events) == 1
    finally:
        await second.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_binding", ["goal", "scope", "model", "durability", "permission"])
async def test_cold_restore_rejects_changed_binding_before_model_or_tools(
    tmp_path, monkeypatch, changed_binding
):
    root, original, _scope, _approval = await _suspend_product(tmp_path, monkeypatch)
    if changed_binding == "durability":
        monkeypatch.setenv("KSADK_STUDIO_NATIVE_DURABLE", "0")
    client = RecordingClient("must not run")
    second = StudioService(root, model_client=client, native_harness_agent_ids=("durable-agent",))
    try:
        if changed_binding == "goal":
            original.input = "Different user objective."
        elif changed_binding == "scope":
            original.runtime_handle["native_ref"]["scope_digest"] = "foreign-scope"
        elif changed_binding == "model":
            original.model = "different-model"
        elif changed_binding == "permission":
            original.native_runtime_options = NativeRunOptions(tool_approval_mode="full")
        second.event_store.save(original)
        await second.start()
        await second.run_service.recover_interrupted(second.resolve_run_spec)
        assert second.event_store.get(original.id).status == RunStatus.INTERRUPTED
        assert client.requests == []
        assert not list(root.rglob("approved.txt"))
        assert not second.run_service._recovery_tasks
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_ordinary_studio_waits_for_crashed_lease_before_restoring_approval(
    tmp_path, monkeypatch
):
    root, original, scope, _approval = await _suspend_product(tmp_path, monkeypatch)
    client = RecordingClient("must wait for approval")
    second = StudioService(root, model_client=client, native_harness_agent_ids=("durable-agent",))
    try:
        await second.start()
        from ksadk.harness.stores import native_sqlite

        now = [native_sqlite.time()]
        monkeypatch.setattr(native_sqlite, "time", lambda: now[0])
        second._native_product_composer.store.acquire_lease(
            scope, original.runtime_handle["run_id"], owner_id="crashed-owner", ttl_seconds=0.7
        )
        await second.run_service.recover_interrupted(second.resolve_run_spec)
        assert second.event_store.get(original.id).status == RunStatus.WAITING_INPUT
        assert second.run_service._waiting_modes.get(original.id) is None
        # Restore preparation may perform slow I/O. Expire the owned fixture
        # lease explicitly only after proving it prevented early attachment.
        now[0] += 0.8
        await _wait_for(lambda: second.run_service._waiting_modes.get(original.id) == "resume")
        assert client.requests == []
        assert not list(root.rglob("approved.txt"))
        assert second.event_store.get(original.id).runtime_handle == original.runtime_handle
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_stopping_restored_run_never_executes_the_pending_tool(tmp_path, monkeypatch):
    root, original, _scope, _approval = await _suspend_product(tmp_path, monkeypatch)
    client = RecordingClient("must not run")
    second = StudioService(root, model_client=client, native_harness_agent_ids=("durable-agent",))
    try:
        await second.start()
        await second.run_service.recover_interrupted(second.resolve_run_spec)
        await _wait_for(lambda: second.run_service._waiting_modes.get(original.id) == "resume")
        await second.run_service.cancel_run(original.id)
        await _wait_for(lambda: not second.run_service._recovery_tasks)
        assert second.event_store.get(original.id).status == RunStatus.CANCELLED
        assert not list(root.rglob("approved.txt"))
        assert client.requests == []
        events = second.event_store.events(original.id)
        assert sum(event.type == "run.cancelled" for event in events) == 1
    finally:
        await second.aclose()
