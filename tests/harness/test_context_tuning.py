from ksadk.harness.context_tuning import context_tuning_recommendations
from ksadk.harness.events import EventType, RuntimeEvent


def _event(event_type: str, seq: int, payload: dict):
    return RuntimeEvent.create(
        event_type,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=seq,
        payload=payload,
    )


def test_no_evidence_produces_no_speculative_recommendation():
    report = context_tuning_recommendations([])
    assert report == {
        "schema_version": 1,
        "status": "healthy",
        "auto_apply": False,
        "recommendation_count": 0,
        "recommendations": [],
    }


def test_recommendations_use_metrics_and_never_expose_raw_content():
    events = [
        _event(
            EventType.CONTEXT_PLANNED,
            1,
            {"budget_tokens": 100, "sections": {"stable_prompt": 80}},
        ),
        _event(
            EventType.CONTEXT_BUILT,
            2,
            {
                "manifest_id": "ctxm-1",
                "planned_tokens": 80,
                "projected_tokens": 120,
                "context_window_tokens": 100,
                "stable_prompt_hash": "sha256:safe",
                "sections": [],
            },
        ),
        _event(
            EventType.CONTEXT_COMPACTION_COMPLETED,
            3,
            {
                "compaction_id": "cmp-1",
                "phase": "proactive",
                "trigger": "budget",
                "compacted_until_seq_id": 2,
                "before_tokens": 200,
                "after_tokens": 80,
                "quality_checks": {"goal_retention": False},
                "reinjected_critical_facts": ["不得泄露的事实"],
            },
        ),
        _event(
            EventType.CONTEXT_RECOVERED,
            4,
            {
                "reason": "compaction_summary_failed:degraded_truncation",
                "error": "内部连接 secret-value 失败",
            },
        ),
    ]
    report = context_tuning_recommendations(events)
    assert report["status"] == "action_required"
    assert report["auto_apply"] is False
    codes = [item["code"] for item in report["recommendations"]]
    assert codes[:2] == ["compaction_quality_failed", "context_budget_exceeded"]
    serialized = __import__("json").dumps(report, ensure_ascii=False)
    assert "不得泄露的事实" not in serialized
    assert "secret-value" not in serialized
