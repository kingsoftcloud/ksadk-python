from __future__ import annotations

import json

import pytest

from ksadk.skills.events import (
    BoundSkillRef,
    SandboxSkillEventEnvelope,
    SkillBinding,
    SkillEvent,
    SkillEventSink,
    SkillExecutionContext,
    build_skill_invocation_plan,
    parse_sandbox_skill_event_lines,
    record_skill_result_consumption,
)
from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.runtime import SkillRuntimeResult


def _ref() -> SkillRef:
    return SkillRef(
        skill_id="skill-1",
        version_id="version-1",
        version="1.0.0",
        name="report",
        content_hash=ContentHash("sha256", "a" * 64),
    )


def test_binding_context_preserves_immutable_candidate_snapshot() -> None:
    binding = SkillBinding(
        binding_snapshot_id="binding-1",
        candidates=(BoundSkillRef(skill_ref=_ref(), space_id="space-1"),),
    )

    context = SkillExecutionContext(run_id="run-1", binding=binding, decision_id="decision-1")

    assert context.binding is binding
    assert context.binding.candidates[0].skill_ref.version == "1.0.0"
    with pytest.raises(AttributeError):
        context.binding.binding_snapshot_id = "other"  # type: ignore[misc]


def test_invocation_plan_rejects_selected_skill_outside_binding() -> None:
    binding = SkillBinding("binding-1", (BoundSkillRef(_ref(), "space-1"),))

    with pytest.raises(ValueError, match="not present in SkillBinding"):
        build_skill_invocation_plan(binding, selected_skill_ids=("other-skill",))


def test_invocation_plan_uses_bound_immutable_skill_ref_once_per_selection() -> None:
    binding = SkillBinding("binding-1", (BoundSkillRef(_ref(), "space-1"),))

    plan = build_skill_invocation_plan(
        binding,
        selected_skill_ids=("skill-1",),
        decision_id="decision-1",
    )

    assert plan.binding_snapshot_id == "binding-1"
    assert plan.decision_id == "decision-1"
    assert plan.entries[0].skill_ref is binding.candidates[0].skill_ref
    assert plan.entries[0].skill_invocation_id.startswith("skill_inv_")


def test_sink_keeps_only_controlled_attributes_without_dropping_event() -> None:
    event = SkillEvent.create(
        "skill.load.completed",
        status="completed",
        skill_ref=_ref(),
        skill_invocation_id="invocation-1",
        attributes={
            "cache_hit": True,
            "download_url": "https://signed.example/archive",
            "prompt": "private request",
            "stdout": "private output",
            "artifact_content": "private artifact",
            "custom": "unreviewed value",
        },
    )

    accepted = SkillEventSink().emit(event)

    assert accepted.attributes == {"cache_hit": True}
    assert accepted.event_id == event.event_id


def test_sandbox_envelope_rejects_mismatched_skill_and_missing_invocation() -> None:
    event = SkillEvent.create(
        "skill.execution.completed",
        status="completed",
        skill_ref=_ref(),
        skill_invocation_id="invocation-1",
        runtime_id="sandbox-1",
    )

    envelope = SandboxSkillEventEnvelope.from_dict(event.to_dict())
    assert (
        envelope.to_event(expected_skill_ref=_ref(), expected_invocation_id="invocation-1") == event
    )

    with pytest.raises(ValueError, match="skill_invocation_id"):
        SandboxSkillEventEnvelope.from_dict({**event.to_dict(), "skill_invocation_id": ""})
    with pytest.raises(ValueError, match="SkillRef"):
        envelope.to_event(
            expected_skill_ref=SkillRef("other", "v", "1", "other"),
            expected_invocation_id="invocation-1",
        )


def test_sandbox_envelope_rejects_invocation_not_in_outer_plan() -> None:
    event = SkillEvent.create(
        "skill.execution.completed",
        status="completed",
        skill_ref=_ref(),
        skill_invocation_id="unplanned",
    )

    parsed = parse_sandbox_skill_event_lines(
        json.dumps(event.to_dict()),
        expected_invocations={"planned": _ref()},
    )

    assert [item.event_type for item in parsed] == ["sandbox.envelope.rejected"]


def test_result_consumption_requires_completed_invocation_and_agent_step() -> None:
    context = SkillExecutionContext(
        run_id="run-1",
        binding=SkillBinding("binding-1", (BoundSkillRef(_ref(), "space-1"),)),
        selection_receipt_id="receipt-1",
    )
    result = SkillRuntimeResult(
        skill_events=[
            SkillEvent.create(
                "skill.execution.completed",
                status="completed",
                skill_ref=_ref(),
                skill_invocation_id="inv-1",
            )
        ]
    )

    receipts = record_skill_result_consumption(
        result, context, agent_step_id="step-1", skill_invocation_ids=("inv-1", "missing")
    )

    assert receipts[0].event_type == "skill.result.consumed"
    assert receipts[0].attributes == {
        "agent_step_id": "step-1",
        "selection_receipt_id": "receipt-1",
    }
    assert receipts[1].error_category == "invalid_consumption_receipt"
