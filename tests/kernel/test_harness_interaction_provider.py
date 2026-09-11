from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ksadk.harness.managed_runtime import managed_harness_capabilities
from ksadk.interaction.contracts import InteractionRecord, InteractionSubmission
from ksadk.interaction.provider import InteractionResolveContext
from ksadk.interaction.providers import default_interaction_providers
from ksadk.runtime import CancelResult, RunHandle


def fixture():
    handle = RunHandle(
        run_id="original-run",
        session_id="session",
        runtime_type="harness",
        native_ref={"thread_id": "original-thread"},
    )
    adapter = SimpleNamespace(
        capabilities=lambda: managed_harness_capabilities(durable=True),
        resume=AsyncMock(return_value=handle),
        cancel=AsyncMock(return_value=CancelResult.INTERRUPTED_ACTIVE_TURN),
    )
    context = InteractionResolveContext(
        adapter=adapter, handle=handle, activation_id="activation", fencing_token=4
    )
    record = InteractionRecord(
        interaction_id="approval",
        tenant_id="tenant",
        agent_instance_id="agent",
        session_id="session",
        run_id=handle.run_id,
        kind="approval",
        request_schema={},
        created_at="2026-09-10T00:00:00Z",
        provider_id="harness",
        native_target={"thread_id": "original-thread", "call_id": "original-call"},
    )
    return adapter, context, record


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_action_maps_to_exact_original_checkpoint_and_call(action):
    adapter, context, record = fixture()
    submission = InteractionSubmission(
        interaction_id=record.interaction_id,
        expected_revision=1,
        action=action,
        response={},
        idempotency_key="decision",
    )
    result = await default_interaction_providers()["harness"].resolve(context, record, submission)
    assert result is context.handle
    handle, target, payload = adapter.resume.call_args.args
    assert (
        handle is context.handle and target.kind == "thread_id" and target.id == "original-thread"
    )
    assert payload.call_id == "original-call" and payload.data == {"decision": action}


@pytest.mark.asyncio
async def test_scope_mismatch_and_cancel_failure_do_not_report_success():
    adapter, context, record = fixture()
    provider = default_interaction_providers()["harness"]
    submission = InteractionSubmission(
        interaction_id=record.interaction_id,
        expected_revision=1,
        action="cancel",
        idempotency_key="cancel",
    )
    with pytest.raises(ValueError, match="original checkpoint"):
        await provider.resolve(
            context, record.model_copy(update={"native_target": {"thread_id": "wrong"}}), submission
        )
    adapter.cancel.assert_not_called()
    adapter.cancel.return_value = CancelResult.FAILED
    with pytest.raises(RuntimeError, match="could not cancel"):
        await provider.resolve(context, record, submission)
    adapter.resume.assert_not_called()
