"""Hosted reader acceptance against the actual Runtime Action and storage."""
import asyncio

import httpx
import pytest
from click.testing import CliRunner

from ksadk.cli import hosted_invoke
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.server.routes import sessions as session_routes
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService
from tests.cli.test_hosted_invoke import event, install, invoke


def route_reader(monkeypatch, tmp_path, rows, backend="memory"):
    monkeypatch.setattr(session_routes, "get_runtime_execution", lambda: (None, None))
    service = (InMemorySessionService() if backend == "memory"
               else LocalSessionService(db_path=tmp_path / "events.db"))

    async def seed():
        await service.create_session("agent-test", "user-test", "session-test")
        for row in rows:
            await service.append_event("session-test", SessionEvent(
                id=row["EventId"], event_type=row["EventType"],
                invocation_id=row["InvocationId"], content=row["Content"],
                # Producer timestamps need not be monotonic; cursors are seq IDs.
                timestamp=float(1000 - row["SeqId"]),
            ))

    asyncio.run(seed())
    app = create_runtime_app(RuntimeAppConfig(
        route_groups={"sessions"}, session_service_provider=lambda: service,
    ), configure_runtime_app)

    def read(query):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://runtime.test") as client:
                response = await client.post("/agentengine/api/v1/ListSessionEvents", json=query)
            assert response.status_code == 200, response.text
            return response.json()["Data"]
        return asyncio.run(request())

    return read


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("claim_seq", [1, 200])
def test_cli_reads_all_actual_route_pages_before_approval_or_completion(monkeypatch, tmp_path, backend, claim_seq):
    rows = [event("control.audit", seq) for seq in range(1, 406)]
    rows[claim_seq - 1] = event("control.message_claimed", claim_seq, payload={"message_id": "our-message"})
    rows[claim_seq] = event("control.run_transition", claim_seq + 1, payload={"state": "pending"})
    # Only an early claimed run can own the request on page one.
    if claim_seq == 1:
        rows[129] = event("interaction.requested", 130, payload={
            "interaction_id": "resolved-approval", "revision": 1,
            "request": {"kind": "approval", "presentation": {"title": "Already resolved"}},
        })
        rows[198] = event("assistant_stream_delta", 199, text="hello ")
    else:
        rows[201] = event("assistant_stream_delta", 202, text="hello ")
    rows[329] = event("interaction.resolved", 330, payload={"interaction_id": "resolved-approval"})
    rows[399] = event("assistant_stream_delta", 400, text="world")
    rows[404] = event("run.completed", 405)
    calls = install(monkeypatch, route_reader(monkeypatch, tmp_path, rows, backend), receipt={
        "Status": "accepted", "CommandId": "our-command", "MessageId": "our-message", "RunId": None,
    })
    result = CliRunner().invoke(invoke)
    assert result.exit_code == 0, result.output
    assert "hello world" in result.output
    assert "Already resolved" not in result.output
    assert not any(name == "SubmitInteraction" for name, _ in calls)
    assert sum(name == "RunAgent" for name, _ in calls) == 1
    queries = [body for name, body in calls if name == "ListSessionEvents"]
    assert [query["AfterSeqId"] for query in queries] == [0, 200, 400]
    assert [query.get("CursorSource") for query in queries] == [None, "runtime", "runtime"]
    assert queries[1]["HistoryEpoch"] == queries[2]["HistoryEpoch"]
    assert queries[1]["HistoryEpoch"]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_actual_route_retains_legacy_tail_and_offset_contract(monkeypatch, tmp_path, backend):
    read = route_reader(monkeypatch, tmp_path, [event("control.audit", n) for n in range(1, 206)], backend)
    # Legacy order remains timestamp DESC; in this fixture timestamps decrease.
    tail = read({"SessionId": "session-test", "Limit": 2, "Offset": 1})
    assert [row["SeqId"] for row in tail["Events"]] == [2, 3]
    assert tail["Total"] == 205 and tail["Offset"] == 1 and tail["Limit"] == 2
    assert "EventOrder" not in tail
    epoch = tail["HistoryEpoch"]
    forward = read({"SessionId": "session-test", "AfterSeqId": 200, "Limit": 2, "EventOrder": "session_seq", "HistoryEpoch": epoch})
    assert [row["SeqId"] for row in forward["Events"]] == [201, 202]
    assert forward["HasMore"] is True and forward["NextCursor"] == 202
    exhausted = read({"SessionId": "session-test", "AfterSeqId": 205, "EventOrder": "session_seq", "HistoryEpoch": epoch})
    assert exhausted["Events"] == [] and exhausted["HasMore"] is False and exhausted["NextCursor"] == 205


def test_cli_legacy_runtime_fails_clearly_without_advancing_or_reposting(monkeypatch):
    calls = install(monkeypatch, [{"Events": [event("run.completed", 205)]}], forward=False)
    result = CliRunner().invoke(invoke)
    assert result.exit_code != 0
    assert "upgrade Runtime" in result.output and "Run continues" in result.output
    assert [name for name, _ in calls].count("RunAgent") == 1
    assert [body["AfterSeqId"] for name, body in calls if name == "ListSessionEvents"] == [0]


@pytest.mark.parametrize("invalid", [
    {"CursorSource": "control"}, {"NextCursor": 999},
    {"Events": [], "HasMore": True, "NextCursor": 1},
])
def test_cli_rejects_cursor_source_switch_or_invalid_progress(monkeypatch, invalid):
    calls = install(monkeypatch, [
        {"Events": [event("run.started", 1)], "HasMore": True},
        {"Events": [event("control.audit", 2)], **invalid},
    ])
    result = CliRunner().invoke(invoke)
    assert result.exit_code != 0
    assert "cursor source changed" in result.output or "forward event page" in result.output
    assert [name for name, _ in calls].count("RunAgent") == 1


def test_cli_original_observation_deadline_never_reposts_run(monkeypatch):
    calls = install(monkeypatch, [{"Events": [event("run.started", 1)]}])
    ticks = iter([0, 1, 901])
    monkeypatch.setattr(hosted_invoke.time, "monotonic", lambda: next(ticks))
    result = CliRunner().invoke(invoke)
    assert result.exit_code != 0 and "Waiting timed out; run continues" in result.output
    assert [name for name, _ in calls].count("RunAgent") == 1


def test_cli_ignores_other_command_terminal_and_child_terminal(monkeypatch):
    child_message = event("assistant_message", 9, text="child reply")
    child_message["Content"]["runtime_event"] = {"run_id": "run-test", "parent_scope_id": "root"}
    calls = install(monkeypatch, [{"Events": [
        event("control.command_rejected", 1, payload={"command_id": "other-command"}),
        event("control.message_claimed", 2, payload={"message_id": "other-message"}),
        event("control.run_transition", 3, payload={"state": "pending", "run_id": "other-run"}),
        event("run.failed", 4, payload={"run_id": "other-run"}),
        event("control.message_claimed", 5, payload={"message_id": "our-message"}),
        event("control.run_transition", 6, payload={"state": "pending"}),
        event("run.failed", 7, payload={"parent_scope_id": "root"}),
        event("assistant_message", 8, text="our reply"),
        child_message,
        event("run.completed", 10),
    ]}], receipt={"CommandId": "our-command", "MessageId": "our-message"})
    result = CliRunner().invoke(invoke)
    assert result.exit_code == 0 and "our reply" in result.output
    assert "child reply" not in result.output
    assert "Run: other-run" not in result.output
    assert [name for name, _ in calls].count("RunAgent") == 1


def test_cli_does_not_submit_approval_after_original_deadline(monkeypatch):
    calls = install(monkeypatch, [{"Events": [event("interaction.requested", 1, payload={
        "interaction_id": "approval-test", "revision": 1, "request": {"kind": "approval"},
    })]}])
    ticks = iter([0, 1, 901, 902])
    monkeypatch.setattr(hosted_invoke.time, "monotonic", lambda: next(ticks))
    result = CliRunner().invoke(invoke, input="y\n")
    assert result.exit_code != 0 and "Waiting timed out; run continues" in result.output
    assert not any(name == "SubmitInteraction" for name, _ in calls)
    assert [name for name, _ in calls].count("RunAgent") == 1


@pytest.mark.parametrize("extra", [
    {}, {"SessionId": " "}, {"SessionId": "session-test", "Offset": 1},
    {"SessionId": "session-test", "CursorSource": "control"},
])
def test_forward_request_rejects_invalid_scope_or_source(extra):
    from pydantic import ValidationError

    from ksadk.server.routes.models import ListSessionEventsActionRequest

    with pytest.raises(ValidationError):
        ListSessionEventsActionRequest(EventOrder="session_seq", **extra)
