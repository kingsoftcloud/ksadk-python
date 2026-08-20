from __future__ import annotations

import json

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.observability.trajectory import encode_sse, project_trajectory_event


def event(event_type: str, *, payload: dict, **updates) -> RuntimeEvent:
    values = {
        "agent_id": "agent-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "invocation_id": "run-1",
        "seq_id": 1,
        "event_id": f"evt-{event_type}",
        "payload": payload,
    }
    values.update(updates)
    return RuntimeEvent.create(event_type, **values)


def test_project_trajectory_event_uses_stable_tool_record_id():
    begin = event(
        EventType.TOOL_CALL_BEGIN,
        step_id="step-1",
        payload={"call_id": "call-1", "name": "search", "args": {"q": "docs"}},
    )
    end = event(
        EventType.TOOL_CALL_END,
        seq_id=2,
        event_id="evt-tool-end",
        step_id="step-1",
        payload={
            "call_id": "call-1",
            "name": "search",
            "result": {"count": 2},
            "status": "completed",
            "duration_ms": 42,
        },
    )

    projected_begin = project_trajectory_event(begin)
    projected_end = project_trajectory_event(end)

    assert projected_begin["recordId"] == projected_end["recordId"] == "tool:call-1"
    assert projected_begin["category"] == "tool"
    assert projected_begin["status"] == "running"
    assert projected_end["status"] == "completed"
    assert projected_end["durationMs"] == 42
    assert projected_end["details"]["result"] == {"count": 2}


def test_project_trajectory_event_exposes_model_ttft_and_missing_duration():
    first_token = event(
        EventType.MODEL_CALL_FIRST_TOKEN,
        step_id="step-1",
        payload={"model_call_id": "model-1", "ttft_ms": 18},
    )

    projected = project_trajectory_event(first_token)

    assert projected["recordId"] == "assistant:model-1"
    assert projected["category"] == "assistant"
    assert projected["durationMs"] is None
    assert projected["details"]["ttft_ms"] == 18


def test_project_trajectory_event_preserves_user_as_a_business_node():
    projected = project_trajectory_event(
        event(
            "user.message",
            turn_id="turn-1",
            payload={"message_id": "msg-1", "text": "帮我检查这个项目"},
        )
    )

    assert projected["recordId"] == "user:msg-1"
    assert projected["category"] == "user"
    assert projected["summary"] == "帮我检查这个项目"


def test_project_trajectory_event_groups_step_message_facts() -> None:
    events = [
        event(
            EventType.MODEL_CALL_BEGIN,
            step_id="step-1",
            payload={"model_call_id": "model-1", "model": "demo-model"},
        ),
        event(
            EventType.REASONING_DELTA,
            seq_id=2,
            event_id="evt-reasoning",
            step_id="step-1",
            payload={"text": "分析", "model_call_id": "model-1"},
        ),
        event(
            EventType.TEXT_COMPLETED,
            seq_id=3,
            event_id="evt-text",
            step_id="step-1",
            phase="final_answer",
            payload={"text": "回答", "model_call_id": "model-1"},
        ),
        event(
            EventType.USAGE_REPORTED,
            seq_id=4,
            event_id="evt-usage",
            step_id="step-1",
            payload={
                "model_call_id": "model-1",
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
            },
        ),
    ]

    projected = [project_trajectory_event(item) for item in events]

    assert {item["recordId"] for item in projected} == {"assistant:model-1"}
    assert {item["category"] for item in projected} == {"assistant"}
    assert {item["summary"] for item in projected} == {"Message"}


def test_project_trajectory_event_groups_text_stream_by_turn_and_phase():
    first = event(
        EventType.TEXT_DELTA,
        turn_id="turn-1",
        phase="commentary",
        payload={"text": "你"},
    )
    second = event(
        EventType.TEXT_DELTA,
        seq_id=2,
        event_id="evt-text-2",
        turn_id="turn-1",
        phase="commentary",
        payload={"text": "好"},
    )

    projected_first = project_trajectory_event(first)
    projected_second = project_trajectory_event(second)

    assert projected_first["recordId"] == projected_second["recordId"]
    assert projected_first["summary"] == "Message"


def test_project_trajectory_event_separates_model_calls_in_one_step():
    first = event(
        EventType.MODEL_CALL_BEGIN,
        step_id="step-1",
        payload={"model_call_id": "model-1", "model": "demo-model"},
    )
    second = event(
        EventType.MODEL_CALL_BEGIN,
        seq_id=2,
        event_id="evt-model-2",
        step_id="step-1",
        payload={"model_call_id": "model-2", "model": "demo-model"},
    )

    assert project_trajectory_event(first)["recordId"] == "assistant:model-1"
    assert project_trajectory_event(second)["recordId"] == "assistant:model-2"
    assert project_trajectory_event(first)["source"]["event_type"] == "model.call.begin"


def test_project_trajectory_event_groups_text_phases_in_one_message():
    commentary = event(
        EventType.TEXT_DELTA,
        turn_id="turn-1",
        phase="commentary",
        payload={"text": "处理中"},
    )
    answer = event(
        EventType.TEXT_COMPLETED,
        seq_id=2,
        event_id="evt-answer",
        turn_id="turn-1",
        phase="final_answer",
        payload={"text": "完成"},
    )

    assert (
        project_trajectory_event(commentary)["recordId"]
        == project_trajectory_event(answer)["recordId"]
    )


def test_project_trajectory_event_falls_back_to_event_record():
    projected = project_trajectory_event(
        event(EventType.RUN_PROGRESS, payload={"status": "running"})
    )

    assert projected["projectionVersion"] == 1
    assert projected["recordId"] == "system:evt-run.progress"
    assert projected["category"] == "system"


def test_project_trajectory_event_uses_stable_business_record_ids():
    cases = [
        (
            event(
                EventType.CONTEXT_COMPACTION_STARTED,
                turn_id="turn-1",
                payload={"phase": "pre_model", "trigger": "budget"},
            ),
            "context:turn-1:compaction",
            "context",
        ),
        (
            event(
                EventType.APPROVAL_REQUESTED,
                payload={"approval_id": "approval-1", "call_id": "call-1", "kind": "tool"},
            ),
            "approval:approval-1",
            "approval",
        ),
        (
            event(
                EventType.ARTIFACT_CREATED,
                payload={"name": "report.md", "version": "v1"},
            ),
            "artifact:report.md:v1",
            "artifact",
        ),
    ]

    for source, record_id, category in cases:
        projected = project_trajectory_event(source)
        assert projected["recordId"] == record_id
        assert projected["category"] == category
        assert projected["eventId"] == source.event_id
        assert projected["seqId"] == source.seq_id


def test_project_trajectory_event_keeps_a2ui_as_system_and_preserves_raw_source():
    source = event(
        EventType.A2UI_SURFACE_BEGIN,
        payload={"surface_id": "surface-1", "data": {"title": "Confirm"}},
    )

    projected = project_trajectory_event(source)

    assert projected["category"] == "system"
    assert projected["recordId"] == f"system:{source.event_id}"
    assert projected["source"] == source.to_dict()


def test_encode_sse_is_deterministic():
    frame = encode_sse({"seqId": 7, "summary": "完成"}, event_id=7)

    assert frame.startswith("id: 7\nevent: runtime_event\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1]) == {"seqId": 7, "summary": "完成"}
