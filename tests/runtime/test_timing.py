import math

import pytest

from ksadk.conversations.runtime_payloads import build_responses_payload
from ksadk.runtime.timing import extract_timing, normalize_timing


@pytest.mark.parametrize("value", [-1, True, "12", math.nan, math.inf, 10**400])
def test_invalid_measurements_remain_unavailable(value):
    result = normalize_timing({"answer_duration_ms": value})
    assert result == {"answer_duration_ms": None, "phase_status": {"answer": "unavailable"}}


def test_zero_skipped_missing_are_distinct():
    result = normalize_timing(
        {
            "answer_duration_ms": 0,
            "selection_duration_ms": None,
            "skill_load_duration_ms": None,
            "phase_status": {"selection": "skipped"},
        }
    )
    assert result["phase_status"] == {
        "answer": "measured",
        "selection": "skipped",
        "skill_load": "unavailable",
    }
    assert "skill_execution_duration_ms" not in result
    assert extract_timing({"output": {"timing": result}}) == {}


def test_responses_keeps_timing_separate_and_old_payloads_unchanged():
    kwargs = dict(output_text="answer", model="test", session_id="session")
    assert "timing" not in build_responses_payload(**kwargs)
    result = build_responses_payload(**kwargs, timing={"agent_duration_ms": 25})
    assert result["timing"]["agent_duration_ms"] == 25
    assert result["output_text"] == "answer"
    assert result["output"][0]["content"][0]["text"] == "answer"

@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "canceled"])
async def test_failure_and_cancel_semantic_events_forward_timing(monkeypatch, terminal):
    from ksadk.runtime import conversation_execution
    from ksadk.events.canonical import RunStarted, RunFailed, RunCanceled, SourceRef

    source = SourceRef(framework="ksadk", native_run_id="run", metadata={"metrics": {"timing": {"agent_duration_ms": 12.5}}})
    common = dict(schema_version=2, run_id="run", scope_id="scope", source=source)
    started = RunStarted(**common, event_id="start", seq=1, timestamp=1, status="running")
    if terminal == "failed":
        finished = RunFailed(**common, event_id="end", seq=2, timestamp=2, status="failed", error={"code": "failed", "message": "failure", "source": "runtime", "scope_id": "scope"})
    else:
        finished = RunCanceled(**common, event_id="end", seq=2, timestamp=2, status="canceled")

    async def events(**kwargs):
        yield started
        yield finished

    monkeypatch.setattr(conversation_execution, "iter_runtime_conversation_events", events)
    result = [event async for event in conversation_execution.iter_runtime_conversation_semantic_events()]
    assert result[-1]["type"] == ("error" if terminal == "failed" else "cancelled")
    assert result[-1]["timing"]["agent_duration_ms"] == 12.5
