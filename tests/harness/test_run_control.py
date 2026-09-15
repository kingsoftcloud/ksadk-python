from __future__ import annotations

import pytest

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.run_control import (
    AcceptanceCheck,
    ControlAction,
    ExperimentDecision,
    MilestoneSpec,
    RunController,
    RunControlSpec,
    RunLimits,
    RunOutcome,
    StagnationPolicy,
)


def _spec(**limit_overrides):
    limits = {
        "max_wall_seconds": 60,
        "max_model_calls": 4,
        "max_tool_calls": 3,
        "max_total_tokens": 100,
        **limit_overrides,
    }
    return RunControlSpec(
        objective="交付一份经过验证的结果",
        acceptance=(
            AcceptanceCheck(
                check_id="answer",
                kind="text_contains",
                expected="DONE",
            ),
            AcceptanceCheck(
                check_id="artifact",
                kind="artifact_exists",
                expected="report.json",
                required=False,
            ),
        ),
        limits=RunLimits(**limits),
        stagnation=StagnationPolicy(
            action_repeat_threshold=2,
            error_repeat_threshold=2,
            max_replans=1,
        ),
    )


def _event(kind, seq, payload):
    return RuntimeEvent.create(
        kind,
        agent_id="a",
        user_id="u",
        session_id="s",
        invocation_id="r",
        seq_id=seq,
        payload=payload,
    )


def test_budget_is_checked_before_side_effect_and_fail_closed():
    controller = RunController(_spec(max_tool_calls=1))
    assert controller.before_tool("write", {"path": "a"}).action == ControlAction.CLOSE
    decision = controller.before_tool("write", {"path": "b"})
    assert decision.action == ControlAction.STOP
    assert decision.reason == "tool_calls_hard_limit"
    assert controller.tool_calls == 1
    assert controller.budget_exhausted is True


def test_soft_limit_requests_close_without_exceeding_hard_limit():
    controller = RunController(_spec(max_model_calls=4))
    assert controller.before_model().action == ControlAction.CONTINUE
    assert controller.before_model().action == ControlAction.CONTINUE
    assert controller.before_model().action == ControlAction.CONTINUE
    decision = controller.before_model()
    assert decision.action == ControlAction.CLOSE
    assert controller.model_calls == 4


def test_repeated_action_replans_once_then_stops_before_execution():
    controller = RunController(_spec())
    assert controller.before_tool("search", {"q": "same"}).action == ControlAction.CONTINUE
    assert controller.before_tool("search", {"q": "same"}).action == ControlAction.REPLAN
    decision = controller.before_tool("search", {"q": "same"})
    assert decision.action == ControlAction.STOP
    assert "stagnation_after_1_replans" in decision.reason
    assert controller.tool_calls == 1


def test_polling_can_explicitly_opt_out_of_stagnation_detection():
    controller = RunController(_spec(max_tool_calls=3))
    for _ in range(3):
        assert controller.before_tool(
            "poll_job", {"job": "1"}, stagnation_exempt=True
        ).action in {ControlAction.CONTINUE, ControlAction.CLOSE}
    assert controller.replans == 0


def test_repeated_structured_error_triggers_replan():
    controller = RunController(_spec())
    error = _event(
        EventType.TOOL_CALL_END,
        1,
        {"call_id": "c1", "name": "lookup", "error": "timeout", "error_category": "timeout"},
    )
    assert controller.observe(error).action == ControlAction.CONTINUE
    error2 = error.model_copy(update={"event_id": "evt_2", "seq_id": 2})
    assert controller.observe(error2).action == ControlAction.REPLAN


def test_snapshot_restore_preserves_budget_and_stagnation_state():
    controller = RunController(_spec())
    controller.before_model()
    controller.before_tool("search", {"q": "same"})
    accepted = _event(EventType.TEXT_COMPLETED, 3, {"text": "DONE"})
    controller.observe(accepted)
    snapshot = controller.snapshot()

    restored = RunController(_spec())
    restored.restore(snapshot)
    assert restored.model_calls == 1
    assert restored.tool_calls == 1
    assert restored.before_tool("search", {"q": "same"}).action == ControlAction.REPLAN
    assert restored.snapshot()["elapsed_seconds"] >= snapshot["elapsed_seconds"]
    result = restored.finalize([])
    assert result.outcome == RunOutcome.VERIFIED
    assert result.evidence_refs == (accepted.event_id,)


def test_snapshot_rejects_different_control_contract():
    controller = RunController(_spec())
    changed = RunController(_spec(max_total_tokens=99))
    with pytest.raises(ValueError, match="digest mismatch"):
        changed.restore(controller.snapshot())


def test_external_acceptance_latch_controls_outcome():
    controller = RunController(_spec())
    events = [
        _event(EventType.TEXT_COMPLETED, 1, {"text": "DONE"}),
        _event(
            EventType.ARTIFACT_CREATED,
            2,
            {"name": "report.json", "version": 1},
        ),
    ]
    result = controller.finalize(events)
    assert result.outcome == RunOutcome.VERIFIED
    assert result.resumable is False
    assert result.evidence_refs == (events[0].event_id, events[1].event_id)
    assert result.to_payload()["control_spec_digest"] == controller.spec.digest


def test_normal_runner_completion_without_acceptance_is_unverified():
    result = RunController(_spec()).finalize([])
    assert result.outcome == RunOutcome.UNVERIFIED
    assert result.resumable is False


def test_budget_exhaustion_is_not_reported_as_generic_failure():
    controller = RunController(_spec(max_total_tokens=10))
    decision = controller.observe(
        _event(
            EventType.USAGE_REPORTED,
            1,
            {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
        )
    )
    assert decision.action == ControlAction.STOP
    result = controller.finalize([])
    assert result.outcome == RunOutcome.BUDGET_EXHAUSTED
    assert result.usage["total_tokens"] == 11


def test_usage_without_total_uses_input_plus_output():
    controller = RunController(_spec(max_total_tokens=20))
    canonical = _event(
        EventType.USAGE_REPORTED,
        1,
        {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    )
    # 兼容进入控制器前尚未完成 canonical 投影的 Provider Usage。
    controller.observe(
        canonical.model_copy(
            update={"payload": {"input_tokens": 7, "output_tokens": 3}}
        )
    )
    assert controller.total_tokens == 10


def test_elapsed_wall_budget_survives_restore(monkeypatch):
    controller = RunController(_spec(max_wall_seconds=1))
    snapshot = controller.snapshot()
    snapshot["elapsed_seconds"] = 2
    restored = RunController(_spec(max_wall_seconds=1))
    restored.restore(snapshot)
    assert restored.before_model().action == ControlAction.STOP


def test_spec_requires_hard_limit_and_unique_acceptance():
    with pytest.raises(ValueError, match="hard limit"):
        RunLimits()
    duplicate = AcceptanceCheck(
        check_id="same", kind="event_exists", expected="run.completed"
    )
    with pytest.raises(ValueError, match="unique"):
        RunControlSpec(
            objective="x",
            acceptance=(duplicate, duplicate),
            limits=RunLimits(max_model_calls=1),
        )


def test_milestones_advance_only_from_external_acceptance_evidence():
    spec = RunControlSpec(
        objective="two milestones",
        acceptance=(
            AcceptanceCheck(check_id="draft", kind="text_contains", expected="DRAFT"),
            AcceptanceCheck(check_id="report", kind="artifact_exists", expected="report.json"),
        ),
        milestones=(
            MilestoneSpec(
                milestone_id="m1",
                description="produce draft",
                acceptance_ids=("draft",),
            ),
            MilestoneSpec(
                milestone_id="m2",
                description="publish report",
                acceptance_ids=("report",),
                depends_on=("m1",),
            ),
        ),
        limits=RunLimits(max_model_calls=5),
    )
    controller = RunController(spec)
    assert controller.next_milestone().milestone_id == "m1"
    first_guidance = controller.take_execution_instruction()
    assert "m1" in first_guidance
    assert controller.take_execution_instruction() is None

    controller.observe(_event(EventType.TEXT_COMPLETED, 1, {"text": "DRAFT"}))
    assert controller.next_milestone().milestone_id == "m2"
    assert "m2" in controller.take_execution_instruction()

    controller.observe(
        _event(EventType.ARTIFACT_CREATED, 2, {"name": "report.json", "version": 1})
    )
    assert controller.next_milestone() is None
    assert controller.finalize([]).completed_milestones == ("m1", "m2")


def test_experiment_ledger_keeps_improvement_and_rolls_back_regression():
    controller = RunController(_spec())
    assert (
        controller.record_experiment(
            "candidate-a",
            baseline_score=0.8,
            candidate_score=0.85,
            minimum_improvement=0.02,
            evidence_refs=("artifact://eval/a",),
        )
        == ExperimentDecision.KEEP
    )
    assert (
        controller.record_experiment(
            "candidate-b",
            baseline_score=0.8,
            candidate_score=0.79,
        )
        == ExperimentDecision.ROLLBACK
    )
    snapshot = controller.snapshot()
    restored = RunController(_spec())
    restored.restore(snapshot)
    assert [item["decision"] for item in restored.experiments] == ["keep", "rollback"]
    with pytest.raises(ValueError, match="duplicate"):
        controller.record_experiment(
            "candidate-a", baseline_score=0.8, candidate_score=0.9
        )
    with pytest.raises(ValueError, match="finite"):
        controller.record_experiment(
            "candidate-c", baseline_score=0.8, candidate_score=float("nan")
        )


def test_milestone_contract_rejects_unknown_checks_and_cycles():
    check = AcceptanceCheck(check_id="done", kind="text_contains", expected="DONE")
    with pytest.raises(ValueError, match="unknown acceptance"):
        RunControlSpec(
            objective="bad",
            acceptance=(check,),
            milestones=(
                MilestoneSpec(
                    milestone_id="m1",
                    description="bad",
                    acceptance_ids=("missing",),
                ),
            ),
            limits=RunLimits(max_model_calls=1),
        )
    with pytest.raises(ValueError, match="acyclic"):
        RunControlSpec(
            objective="cycle",
            acceptance=(check,),
            milestones=(
                MilestoneSpec(
                    milestone_id="m1",
                    description="one",
                    acceptance_ids=("done",),
                    depends_on=("m2",),
                ),
                MilestoneSpec(
                    milestone_id="m2",
                    description="two",
                    acceptance_ids=("done",),
                    depends_on=("m1",),
                ),
            ),
            limits=RunLimits(max_model_calls=1),
        )
