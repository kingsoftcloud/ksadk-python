from __future__ import annotations

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.skill_feedback import skill_feedback


def _event(event_type: str, seq: int, payload: dict) -> RuntimeEvent:
    return RuntimeEvent.create(
        event_type,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id="run-1",
        seq_id=seq,
        timestamp=float(seq),
        payload=payload,
    )


def test_feedback_projects_full_progression_without_content_or_results():
    ref = "skill://budget@1.0.0"
    events = []
    seq = 0
    for name, level in (
        ("skill_read_manifest", 1),
        ("skill_read_instructions", 2),
        ("skill_read_resource", 3),
    ):
        seq += 1
        events.append(
            _event(
                EventType.TOOL_CALL_BEGIN,
                seq,
                {"call_id": f"call-{level}", "name": name, "args": {"skill_id": ref}},
            )
        )
        seq += 1
        events.append(
            _event(
                EventType.SKILL_DISCLOSED,
                seq,
                {
                    "skill_ref": ref,
                    "level": level,
                    "content_hash": f"sha256:{level}",
                    "size_bytes": level * 10,
                    "recommended": True,
                },
            )
        )
        seq += 1
        events.append(
            _event(
                EventType.TOOL_CALL_END,
                seq,
                {
                    "call_id": f"call-{level}",
                    "name": name,
                    "result": "must-not-leak",
                },
            )
        )

    report = skill_feedback(events, bound_skill_refs=(ref, "skill://unused@1.0.0"))

    assert report["schemaVersion"] == 1
    assert report["boundCount"] == 2
    assert report["usedCount"] == 1
    used = next(item for item in report["items"] if item["skill_ref"] == ref)
    assert used["levels_reached"] == [1, 2, 3]
    assert used["completed_progression"] is True
    assert used["successful_calls"] == 3
    assert used["disclosed_bytes"] == 60
    assert "must-not-leak" not in str(report)


def test_feedback_tracks_invalid_calls_and_unused_bound_skills():
    ref = "skill://expense@2.0.0"
    events = [
        _event(
            EventType.TOOL_CALL_BEGIN,
            1,
            {
                "call_id": "bad",
                "name": "skill_read_instructions",
                "arguments": {"skill_id": ref},
            },
        ),
        _event(
            EventType.TOOL_CALL_END,
            2,
            {
                "call_id": "bad",
                "name": "skill_read_instructions",
                "error": "must read manifest first",
            },
        ),
    ]

    report = skill_feedback(events, bound_skill_refs=(ref, "skill://unused@1.0.0"))
    expense = next(item for item in report["items"] if item["skill_ref"] == ref)
    unused = next(
        item for item in report["items"] if item["skill_ref"] == "skill://unused@1.0.0"
    )

    assert report["failedCallCount"] == 1
    assert expense["invalid_call_rate"] == 1.0
    assert expense["used"] is False
    assert unused["disclosure_calls"] == 0
    assert unused["used"] is False


def test_feedback_marks_deep_disclosure_without_resource_as_signal_not_failure():
    ref = "skill://report@1.0.0"
    report = skill_feedback(
        [
            _event(
                EventType.SKILL_DISCLOSED,
                1,
                {
                    "skill_ref": ref,
                    "level": 1,
                    "size_bytes": 10,
                    "content_hash": "sha256:one",
                },
            ),
            _event(
                EventType.SKILL_DISCLOSED,
                2,
                {
                    "skill_ref": ref,
                    "level": 2,
                    "size_bytes": 30,
                    "content_hash": "sha256:two",
                },
            ),
        ]
    )

    assert report["items"][0]["deep_disclosure_without_resource"] is True
    assert report["items"][0]["failed_calls"] == 0
