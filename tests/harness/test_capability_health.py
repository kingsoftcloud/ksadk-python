"""MCP / Skill / Sandbox 统一健康快照合同。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.insights import HarnessInsightsRegistry, mount_insights
from ksadk.harness.observability import capability_health


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


def test_projects_mcp_skill_and_sandbox_without_raw_errors():
    events = [
        _event(
            EventType.CAPABILITY_DEGRADED,
            1,
            {
                "capability_ref": "mcp://finance@1",
                "kind": "mcp",
                "operation": "tools/list",
                "reason": "https://private.example?token=secret",
            },
        ),
        _event(
            EventType.CAPABILITY_RECOVERED,
            2,
            {
                "capability_ref": "mcp://finance@1",
                "kind": "mcp",
                "operation": "tools/list",
                "reason": "operation_succeeded",
            },
        ),
        _event(
            EventType.SKILL_DISCLOSED,
            3,
            {
                "skill_ref": "skill://budget-review@1",
                "level": 2,
                "content_hash": "sha256:skill",
                "size_bytes": 12,
            },
        ),
        _event(
            EventType.TOOL_CALL_BEGIN,
            4,
            {"call_id": "sandbox-1", "name": "sandbox_read_file", "args": {}},
        ),
        _event(
            EventType.TOOL_CALL_END,
            5,
            {
                "call_id": "sandbox-1",
                "name": "sandbox_read_file",
                "error": "TimeoutError: private path /workspace/customer-a",
            },
        ),
    ]

    snapshot = capability_health(events)

    assert snapshot["overall_status"] == "degraded"
    assert snapshot["counts"] == {"available": 2, "degraded": 1, "unknown": 0}
    by_kind = {item["kind"]: item for item in snapshot["items"]}
    assert by_kind["mcp"]["status"] == "available"
    assert by_kind["mcp"]["transition_count"] == 2
    assert by_kind["mcp"]["reason_code"] == "operation_succeeded"
    assert by_kind["skill"]["operation"] == "disclosure_l2"
    assert by_kind["sandbox"]["reason_code"] == "TimeoutError"
    assert "private" not in str(snapshot)
    assert "secret" not in str(snapshot)


def test_skill_failure_uses_paired_begin_arguments_and_can_recover():
    events = [
        _event(
            EventType.TOOL_CALL_BEGIN,
            1,
            {
                "call_id": "skill-1",
                "name": "skill_read_manifest",
                "args": {"skill_id": "skill://invoice@3"},
            },
        ),
        _event(
            EventType.TOOL_CALL_END,
            2,
            {
                "call_id": "skill-1",
                "name": "skill_read_manifest",
                "error": "FileNotFoundError: /private/skill/SKILL.md",
            },
        ),
        _event(
            EventType.SKILL_DISCLOSED,
            3,
            {
                "skill_ref": "skill://invoice@3",
                "level": 1,
                "content_hash": "sha256:manifest",
                "size_bytes": 42,
            },
        ),
    ]

    item = capability_health(events)["items"][0]
    assert item["capability_ref"] == "skill://invoice@3"
    assert item["status"] == "available"
    assert item["transition_count"] == 2
    assert "private" not in str(item)


def test_policy_denial_does_not_mark_sandbox_degraded():
    events = [
        _event(
            EventType.TOOL_CALL_BEGIN,
            1,
            {"call_id": "sandbox-1", "name": "sandbox_run_command", "args": {}},
        ),
        _event(
            EventType.TOOL_CALL_END,
            2,
            {
                "call_id": "sandbox-1",
                "name": "sandbox_run_command",
                "error": "approval denied",
            },
        ),
    ]
    assert capability_health(events) == {
        "overall_status": "unknown",
        "counts": {"available": 0, "degraded": 0, "unknown": 0},
        "items": [],
    }


def test_capability_health_http_run_and_session_contracts():
    registry = HarnessInsightsRegistry()
    event = _event(
        EventType.MCP_DISCLOSED,
        1,
        {
            "server_id": "mcp://finance@1",
            "level": 1,
            "content_hash": "sha256:tools",
            "size_bytes": 64,
        },
    )
    registry.record("session-1", "run-1", event)
    app = FastAPI()
    mount_insights(app, registry)

    with TestClient(app) as client:
        run = client.get("/insights/runs/run-1/capability-health")
        session = client.get("/insights/sessions/session-1/capability-health")
        missing = client.get("/insights/runs/missing/capability-health")

    assert run.status_code == 200
    assert session.status_code == 200
    assert run.json() == session.json()
    assert run.json()["items"][0]["status"] == "available"
    assert missing.status_code == 404
