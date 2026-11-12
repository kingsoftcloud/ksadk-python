"""Custom A2UI layout still resumes the ordinary native approval, never a new run."""

from datetime import datetime, timezone

import pytest
from langgraph.types import interrupt

from ksadk.a2ui import build_a2ui_presentation
from ksadk.interaction.contracts import InteractionRecord, InteractionSubmission
from ksadk.interaction.native_events import NativeSubmissionProgress
from ksadk.interaction.provider import InteractionResolveContext
from ksadk.interaction.providers.langgraph import LangGraphInteractionProvider
from tests.e2e.a2ui_runtime_app import approval_operations
from tests.interaction.test_native_confirmation_pipeline import start


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_declared_a2ui_button_uses_canonical_native_approval(tmp_path, legacy, decision):
    operations = approval_operations("review", "Review local operation", legacy=legacy)
    presentation = build_a2ui_presentation(operations)
    effects = []

    def wait(_state):
        answer = interrupt({"kind": "approval", "presentation": presentation})
        if answer["decision"] == "approve":
            effects.append("local-effect")
        return {"answer": answer["decision"]}

    runner, adapter, handle, request, starts = await start(tmp_path, wait)
    try:
        assert request.interaction_kind == "approval"
        assert request._presentation == presentation
        record = InteractionRecord(
            interaction_id=request.interaction_id, tenant_id="test", agent_instance_id="test",
            session_id="s", run_id=request.run_id, kind="approval",
            request_schema={"type": "object", "properties": {
                "decision": {"enum": ["approve", "reject"]}}, "required": ["decision"]},
            presentation=presentation, provider_id="langgraph",
            native_target=request._native_target,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await LangGraphInteractionProvider().resolve(
            InteractionResolveContext(adapter, handle, "test", 1), record,
            InteractionSubmission(
                interaction_id=request.interaction_id, expected_revision=1, action=decision,
                response={"decision": decision}, idempotency_key="a2ui-decision",
            ),
        )
        events = [event async for event in adapter.stream(handle)]
        outcome = next(event for event in events if isinstance(event, NativeSubmissionProgress))
        assert outcome._outcomes[0].status == "consumed"
        assert starts == ["before"]
        assert effects == (["local-effect"] if decision == "approve" else [])
        assert events[-1].event_type == "run.completed"
        snapshot = await runner._agent.aget_state({
            "configurable": {"thread_id": request._native_target["thread_id"]},
        })
        assert snapshot.values["answer"] == decision
        assert not snapshot.interrupts and not snapshot.next
    finally:
        await runner.close()
