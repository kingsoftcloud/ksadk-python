import pytest

from ksadk.evaluation.contracts import DataPolicy
from ksadk.evaluation.evidence import EvidenceStore, EvidenceStoreError, project_tool_calls
from ksadk.events.runtime_event import EventType, RuntimeEvent


def _event(event_type: str, seq_id: int, payload: dict) -> RuntimeEvent:
    return RuntimeEvent.create(
        event_type,
        agent_id="agent",
        user_id="eval-user",
        session_id="session-1",
        invocation_id="invocation-1",
        seq_id=seq_id,
        payload=payload,
    )


def test_project_tool_calls_pairs_begin_and_end_events() -> None:
    projected = project_tool_calls(
        [
            _event(
                EventType.TOOL_CALL_BEGIN,
                2,
                {"call_id": "call-1", "name": "lookup", "args": {"query": "secret"}},
            ),
            _event(
                EventType.TOOL_CALL_END,
                3,
                {"call_id": "call-1", "name": "lookup", "result": "private"},
            ),
        ]
    )

    assert [item.model_dump() for item in projected] == [
        {
            "call_id": "call-1",
            "name": "lookup",
            "status": "SUCCEEDED",
            "seq_start": 2,
            "seq_end": 3,
        }
    ]


def test_project_tool_calls_preserves_incomplete_and_failed_calls() -> None:
    projected = project_tool_calls(
        [
            _event(
                EventType.TOOL_CALL_BEGIN,
                4,
                {"call_id": "call-open", "name": "open", "args": {}},
            ),
            _event(
                EventType.TOOL_CALL_END,
                7,
                {
                    "call_id": "call-failed",
                    "name": "write",
                    "error": "denied",
                },
            ),
        ]
    )

    assert [(item.call_id, item.status) for item in projected] == [
        ("call-open", "INCOMPLETE"),
        ("call-failed", "ERROR"),
    ]


@pytest.mark.parametrize(
    ("policy", "expected_text", "expected_args"),
    [
        (DataPolicy.LOCAL_ONLY, "private answer", {"token": "secret"}),
        (DataPolicy.FULL_TRACE, "private answer", {"token": "secret"}),
        (DataPolicy.REDACTED_TRACE, "[REDACTED]", "[REDACTED]"),
        (DataPolicy.METADATA_ONLY, None, None),
    ],
)
def test_evidence_store_applies_data_policy_and_returns_queryable_trace(
    tmp_path,
    policy: DataPolicy,
    expected_text,
    expected_args,
) -> None:
    events = [
        _event(EventType.RUN_STARTED, 1, {"status": "in_progress"}),
        _event(
            EventType.TOOL_CALL_BEGIN,
            2,
            {"call_id": "call-1", "name": "lookup", "args": {"token": "secret"}},
        ),
        RuntimeEvent.create(
            EventType.TEXT_COMPLETED,
            agent_id="agent",
            user_id="eval-user",
            session_id="session-1",
            invocation_id="invocation-1",
            seq_id=3,
            phase="final_answer",
            payload={"text": "private answer"},
        ),
        _event(EventType.RUN_COMPLETED, 4, {"status": "completed"}),
    ]
    store = EvidenceStore(tmp_path)

    trace_ref = store.write_trace("eval-1", events, policy=policy)
    trace = store.read_trace(trace_ref)

    assert trace_ref.run_id == "eval-1"
    assert trace_ref.session_id == "session-1"
    assert trace_ref.invocation_id == "invocation-1"
    assert (trace_ref.seq_start, trace_ref.seq_end) == (1, 4)
    text_event = next(item for item in trace["events"] if item["eventType"] == "text.completed")
    tool_event = next(item for item in trace["events"] if item["eventType"] == "tool.call.begin")
    assert text_event.get("payload", {}).get("text") == expected_text
    assert tool_event.get("payload", {}).get("args") == expected_args
    assert "secret" not in repr(trace) or policy in {
        DataPolicy.LOCAL_ONLY,
        DataPolicy.FULL_TRACE,
    }


def test_evidence_store_rejects_unscoped_or_escaped_reads(tmp_path) -> None:
    store = EvidenceStore(tmp_path)

    with pytest.raises(EvidenceStoreError):
        store.write_trace("../outside", [_event(EventType.RUN_STARTED, 1, {"status": "x"})])
