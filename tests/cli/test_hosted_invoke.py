"""Receipt transport regression: never treat admission or an empty stream as success."""
import click
import pytest
from click.testing import CliRunner

from ksadk.cli import hosted_invoke


def event(kind, seq, *, payload=None, text=None):
    return {"EventId": str(seq), "SeqId": seq, "InvocationId": "run-test",
            "EventType": kind, "Content": {"text": text} if text else {
                "session_event": {"payload": {"run_id": "run-test", **(payload or {})}}}}


def install(monkeypatch, pages, *, kernel=True, existing=False, receipt=None, forward=True):
    calls = []

    class Response:
        is_redirect = False
        status_code = 200

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class Transport:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, *, json, **kwargs):
            name = url.rsplit("/", 1)[-1]
            calls.append((name, json))
            assert url.startswith("https://agent.example/agentengine/api/v1/")
            assert kwargs["allow_redirects"] is False
            assert self.headers["Authorization"] == "Bearer fake"
            if name == "GetSession" and not existing:
                return Response({"Code": 404, "Message": "missing"})
            values = {
                "GetAgentUiBootstrap": {"Agent": {"AgentId": "agent-test"},
                                        "HostedRuntime": {}, "Capabilities": {"KernelSessionEvents": kernel}},
                "GetSession": {"Session": {"SessionId": "session-test"}},
                "CreateSession": {"Session": {"SessionId": "session-test"}},
                "RunAgent": receipt or {"Status": "accepted", "RunId": "run-test"},
                "SubmitInteraction": {"Status": "accepted"},
            }
            if name == "ListSessionEvents":
                assert json["EventOrder"] == "session_seq"
                if callable(pages):
                    data = pages(json)
                else:
                    data = pages.pop(0)
                    if forward:
                        data.setdefault("EventOrder", "session_seq")
                        data.setdefault("CursorSource", "runtime")
                        data.setdefault("HasMore", False)
                        data.setdefault("NextCursor", max(
                            (e["SeqId"] for e in data["Events"]), default=json["AfterSeqId"]))
            else:
                data = values[name]
            return Response({"Code": 0, "Data": data})

    monkeypatch.setattr(hosted_invoke.requests, "Session", Transport)
    monkeypatch.setattr(hosted_invoke.time, "sleep", lambda _: None)
    return calls


@click.command()
def invoke():
    result = hosted_invoke.try_invoke_hosted_once(endpoint="https://agent.example", api_key="fake",
                session_id="session-test", message="test", model=None, insecure=False)
    if result is False:
        click.echo("legacy transport")


def test_receipt_waits_for_paginated_result_without_reposting(monkeypatch):
    calls = install(monkeypatch, [
        {"Events": [event("run.started", 1)], "HasMore": True},
        {"Events": [event("assistant_message", 2, text="remote reply"), event("run.completed", 3)]},
    ])
    result = CliRunner().invoke(invoke)
    assert result.exit_code == 0, result.output
    assert "remote reply" in result.output
    assert [n for n, _ in calls].count("RunAgent") == 1
    assert [b["AfterSeqId"] for n, b in calls if n == "ListSessionEvents"] == [0, 1]


@pytest.mark.parametrize("next_epoch", ["epoch-one", "epoch-two", None])
def test_forward_cursor_preserves_history_epoch(monkeypatch, next_epoch):
    calls = install(monkeypatch, [
        {"Events": [event("run.started", 1)], "HasMore": True, "HistoryEpoch": "epoch-one"},
        {"Events": [event("assistant_message", 2, text="remote reply"), event("run.completed", 3)],
         "HistoryEpoch": next_epoch},
    ])
    result = CliRunner().invoke(invoke)
    queries = [body for name, body in calls if name == "ListSessionEvents"]
    assert queries[1]["HistoryEpoch"] == "epoch-one"
    assert [name for name, _ in calls].count("RunAgent") == 1
    assert "SubmitInteraction" not in [name for name, _ in calls]
    if next_epoch == "epoch-one":
        assert result.exit_code == 0, result.output
        assert "remote reply" in result.output
    else:
        assert result.exit_code != 0
        assert "history epoch changed" in result.output


def test_queued_receipt_matches_its_own_claim_before_selecting_run(monkeypatch):
    calls = install(monkeypatch, [
        {"Events": [event("control.message_claimed", 1, payload={"message_id": "other-message"}),
                    event("control.run_transition", 2, payload={"state": "pending", "run_id": "other-run"})]},
        {"Events": [event("control.message_claimed", 3, payload={"message_id": "our-message"})]},
        {"Events": [event("control.run_transition", 4, payload={"state": "pending"}),
                    event("assistant_message", 5, text="our reply"), event("run.completed", 6)]},
    ], receipt={"Status": "accepted", "CommandId": "command-test", "MessageId": "our-message", "RunId": None})
    result = CliRunner().invoke(invoke)
    assert result.exit_code == 0, result.output
    assert "our reply" in result.output
    assert "Run: other-run" not in result.output
    assert [n for n, _ in calls].count("RunAgent") == 1


@pytest.mark.parametrize("terminal", ["run.failed", "run.canceled", "run.completed"])
def test_failure_or_empty_completion_is_not_success(monkeypatch, terminal):
    install(monkeypatch, [{"Events": [event(terminal, 1)]}])
    assert CliRunner().invoke(invoke).exit_code != 0


def test_legacy_hosted_agent_keeps_old_transport(monkeypatch):
    calls = install(monkeypatch, [], kernel=False)
    result = CliRunner().invoke(invoke)
    assert result.exit_code == 0
    assert result.output.strip() == "legacy transport"
    assert [n for n, _ in calls] == ["GetAgentUiBootstrap"]


def test_approval_is_explicit_and_session_can_be_reused(monkeypatch):
    pending = event("interaction.requested", 1, payload={"interaction_id": "approval-test", "revision": 1,
                            "request": {"kind": "approval", "presentation": {"title": "Call expert"}}})
    calls = install(monkeypatch, [{"Events": [pending]}, {"Events": [
        event("assistant_message", 2, text="approved reply"), event("run.completed", 3)]}], existing=True)
    result = CliRunner().invoke(invoke, input="y\n")
    assert result.exit_code == 0, result.output
    assert "CreateSession" not in [n for n, _ in calls]
    submits = [b for n, b in calls if n == "SubmitInteraction"]
    assert len(submits) == 1
    assert submits[0]["InteractionAction"] == "approve"
    assert submits[0]["ExpectedRevision"] == 1
    assert submits[0]["IdempotencyKey"].startswith("cli-")


def test_noninteractive_approval_leaves_background_run_waiting(monkeypatch):
    calls = install(monkeypatch, [{"Events": [event("interaction.requested", 1, payload={
        "interaction_id": "approval-test", "revision": 1, "request": {"kind": "approval"}})]}])
    result = CliRunner().invoke(invoke)
    assert result.exit_code != 0
    assert "waiting for approval" in result.output
    assert "SubmitInteraction" not in [n for n, _ in calls]
