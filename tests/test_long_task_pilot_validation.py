import json

import pytest

from scripts import validate_long_task_pilot


@pytest.mark.asyncio
async def test_build_pilot_report_includes_resume_cancel_and_acceptance_metrics(monkeypatch):
    async def fake_run_validation(*, dsn: str, keep_session: bool):
        assert dsn == "postgresql://example"
        assert keep_session is False
        return {
            "session_id": "sess_resume",
            "run_id": "run_resume",
            "checkpoint_id": "ckpt_resume",
            "output_text": "a,b,c",
            "checkpoint_count": 1,
            "run_checkpoint_event_count": 2,
            "run_resume_event_count": 1,
            "checkpoint_log_before_resume": ["a", "b"],
            "node_counts_after_resume": {"a": 1, "b": 1, "c": 1},
            "resume_did_not_rerun_prior_nodes": True,
        }

    async def fake_run_cancel_validation(*, dsn: str, keep_session: bool):
        assert dsn == "postgresql://example"
        assert keep_session is False
        return {
            "session_id": "sess_cancel",
            "invocation_id": "run_cancel",
            "cancel_found": True,
            "cancel_status": "cancelling",
            "cancelled_event_count": 1,
            "post_cancel_extra_event_count": 0,
        }

    monkeypatch.setattr(validate_long_task_pilot, "run_validation", fake_run_validation)
    monkeypatch.setattr(validate_long_task_pilot, "run_cancel_validation", fake_run_cancel_validation)

    report = await validate_long_task_pilot.build_pilot_report(
        dsn="postgresql://example",
        keep_session=False,
        include_cancel=True,
    )

    assert report["overall_status"] == "pass"
    assert report["metrics"]["checkpoint_resume_success_rate"] == 1.0
    assert report["metrics"]["runtime_cancel_success_rate"] == 1.0
    assert report["cases"]["checkpoint_resume"]["status"] == "pass"
    assert report["cases"]["runtime_cancel"]["status"] == "pass"
    assert report["acceptance"]["same_run_id_resume"] == "pass"
    assert report["acceptance"]["resume_does_not_restart"] == "pass"
    assert report["cases"]["checkpoint_resume"]["resume_did_not_rerun_prior_nodes"] is True
    assert report["metrics"]["node_counts_after_resume"] == {"a": 1, "b": 1, "c": 1}
    assert report["acceptance"]["no_events_after_cancel"] == "pass"
    json.dumps(report, ensure_ascii=False)


@pytest.mark.asyncio
async def test_build_pilot_report_marks_failed_cancel_boundary(monkeypatch):
    async def fake_run_validation(*, dsn: str, keep_session: bool):
        return {
            "output_text": "a,b,c",
            "checkpoint_count": 1,
            "run_checkpoint_event_count": 2,
            "run_resume_event_count": 1,
            "checkpoint_log_before_resume": ["a", "b"],
            "node_counts_after_resume": {"a": 1, "b": 1, "c": 1},
            "resume_did_not_rerun_prior_nodes": True,
        }

    async def fake_run_cancel_validation(*, dsn: str, keep_session: bool):
        return {
            "cancel_found": True,
            "cancel_status": "cancelling",
            "cancelled_event_count": 1,
            "post_cancel_extra_event_count": 2,
        }

    monkeypatch.setattr(validate_long_task_pilot, "run_validation", fake_run_validation)
    monkeypatch.setattr(validate_long_task_pilot, "run_cancel_validation", fake_run_cancel_validation)

    report = await validate_long_task_pilot.build_pilot_report(
        dsn="postgresql://example",
        keep_session=False,
        include_cancel=True,
    )

    assert report["overall_status"] == "fail"
    assert report["metrics"]["runtime_cancel_success_rate"] == 0.0
    assert report["acceptance"]["no_events_after_cancel"] == "fail"


@pytest.mark.asyncio
async def test_build_pilot_report_aggregates_multiple_iterations(monkeypatch):
    checkpoint_calls = 0
    cancel_calls = 0

    async def fake_run_validation(*, dsn: str, keep_session: bool):
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        result = {
            "session_id": f"sess_resume_{checkpoint_calls}",
            "run_id": f"run_resume_{checkpoint_calls}",
            "checkpoint_id": f"ckpt_resume_{checkpoint_calls}",
            "output_text": "a,b,c",
            "checkpoint_count": 1,
            "run_checkpoint_event_count": 2,
            "run_resume_event_count": 1,
            "checkpoint_log_before_resume": ["a", "b"],
            "node_counts_after_resume": {"a": 1, "b": 1, "c": 1},
            "resume_did_not_rerun_prior_nodes": True,
        }
        if checkpoint_calls == 2:
            result["resume_did_not_rerun_prior_nodes"] = False
            result["node_counts_after_resume"] = {"a": 2, "b": 2, "c": 1}
        return result

    async def fake_run_cancel_validation(*, dsn: str, keep_session: bool):
        nonlocal cancel_calls
        cancel_calls += 1
        return {
            "session_id": f"sess_cancel_{cancel_calls}",
            "invocation_id": f"run_cancel_{cancel_calls}",
            "cancel_found": True,
            "cancel_status": "cancelling",
            "cancelled_event_count": 1,
            "post_cancel_extra_event_count": 0,
        }

    monkeypatch.setattr(validate_long_task_pilot, "run_validation", fake_run_validation)
    monkeypatch.setattr(validate_long_task_pilot, "run_cancel_validation", fake_run_cancel_validation)

    report = await validate_long_task_pilot.build_pilot_report(
        dsn="postgresql://example",
        keep_session=False,
        include_cancel=True,
        iterations=3,
    )

    assert report["overall_status"] == "fail"
    assert report["metrics"]["total_iterations"] == 3
    assert report["metrics"]["checkpoint_resume_passed"] == 2
    assert report["metrics"]["runtime_cancel_passed"] == 3
    assert report["metrics"]["checkpoint_resume_success_rate"] == pytest.approx(2 / 3)
    assert report["metrics"]["runtime_cancel_success_rate"] == 1.0
    assert len(report["iterations"]) == 3
    assert report["iterations"][1]["cases"]["checkpoint_resume"]["status"] == "fail"
    assert report["cases"]["checkpoint_resume"]["status"] == "fail"
    assert report["acceptance"]["resume_does_not_restart"] == "fail"


@pytest.mark.asyncio
async def test_build_pilot_report_returns_json_failure_when_validation_raises(monkeypatch):
    async def fake_run_validation(*, dsn: str, keep_session: bool):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(validate_long_task_pilot, "run_validation", fake_run_validation)

    report = await validate_long_task_pilot.build_pilot_report(
        dsn="postgresql://example",
        keep_session=False,
        include_cancel=True,
    )

    assert report["overall_status"] == "fail"
    assert report["cases"]["checkpoint_resume"]["status"] == "fail"
    assert report["cases"]["checkpoint_resume"]["error_type"] == "RuntimeError"
    assert "database unavailable" in report["cases"]["checkpoint_resume"]["error"]
    assert report["cases"]["runtime_cancel"]["status"] == "skipped"
    json.dumps(report, ensure_ascii=False)
