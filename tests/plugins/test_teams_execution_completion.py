"""Atomic completion, including real disposable PostgreSQL transactions."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from ksadk.plugins.teams.contracts import Actor, GroupCreateInput, MessageInput, TaskCreateInput
from ksadk.plugins.teams.domain import TeamsDomain
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.execution_projection import completion_result_digest
from ksadk.plugins.teams.store import TeamsStore

OWNER = Actor("tenant-test", "owner-test")
BINDING = {
    "bindingRef": "worker-build",
    "kind": "local_build",
    "buildId": "worker-build",
    "agentId": "test-agent",
    "providerRef": "harness",
    "memberClass": "task_worker",
    "capabilities": {"enqueue": True, "cancel": True, "leader": True, "interaction": True},
}


@pytest.fixture(params=["sqlite", "postgres"])
def domain(request, tmp_path, monkeypatch):
    server = None
    if request.param == "postgres":
        pgserver = pytest.importorskip("pgserver")
        from ksadk.plugins.teams.postgres_store import PostgresTeamsStore

        monkeypatch.setenv("LC_ALL", "C")
        monkeypatch.setenv("LANG", "C")
        server = pgserver.get_server(tmp_path / "postgres", cleanup_mode="stop")
        store = PostgresTeamsStore(server.get_uri(), authority_ref="completion-tests")
    else:
        store = TeamsStore(tmp_path / "teams.sqlite")
    try:
        yield TeamsDomain(store, authority_ref="completion-tests")
    finally:
        store.close()
        if server:
            server.cleanup()


def setup(domain, acceptance="human"):
    created = domain.create_group(
        OWNER,
        GroupCreateInput(
            name="completion",
            members=[
                {"memberId": "leader", "name": "Leader", "bindingRef": "leader-build"},
                {"memberId": "worker", "name": "Worker", "bindingRef": "worker-build"},
            ],
            leaderMemberId="leader",
            idempotencyKey="create",
        ),
        {
            "worker-build": BINDING,
            "leader-build": {**BINDING, "bindingRef": "leader-build", "memberClass": "full_member"},
        },
    )
    group_id = created["group"]["groupId"]
    run_id = domain.send(
        OWNER,
        group_id,
        MessageInput(
            parts=[{"kind": "text", "text": "goal"}], intent="start_goal", idempotencyKey="goal"
        ),
    )["teamRunId"]
    task = domain.create_task(
        OWNER,
        group_id,
        run_id,
        TaskCreateInput(title="task", ownerMemberId="worker", acceptancePolicy=acceptance),
        "task",
    )
    with domain.store.transaction() as tx:
        stored = tx.get("task", task["taskId"])
        delivery = tx.get("delivery", stored["attempts"][-1]["_deliveryId"])
        delivery.update(
            commandId=str(uuid4()), runId="native-run", status="accepted", _tokenLimit=100
        )
        tx.put("delivery", delivery["deliveryId"], delivery)
    candidate = {"result": "最终报告", "artifacts": []}
    evidence = {
        "sessionId": delivery["_sessionId"],
        "runId": "native-run",
        "commandId": delivery["commandId"],
        "terminalSeq": 20,
        "terminalEventDigest": "sha256:" + "a" * 64,
        "resultDigest": completion_result_digest(candidate),
    }
    return group_id, run_id, task, delivery, candidate, evidence


def project(domain, delivery, evidence, candidate, key="final", **kwargs):
    return domain.project_execution_completion(
        delivery_id=delivery["deliveryId"],
        terminal_evidence=evidence,
        status=kwargs.pop("status", "succeeded"),
        candidate=candidate,
        usage=kwargs.pop("usage", {"totalTokens": 15}),
        projection_key=key,
        **kwargs,
    )


def rows(domain, run_id, task, delivery):
    with domain.store.transaction() as tx:
        return (
            tx.get("team_run", run_id),
            tx.get("task", task["taskId"]),
            tx.get("delivery", delivery["deliveryId"]),
        )


def test_terminal_then_result_is_atomic_replayable_and_not_native_success(domain):
    gid, rid, task, delivery, candidate, evidence = setup(domain)
    pending = {**evidence, "resultDigest": None}
    assert project(
        domain, delivery, pending, None, "pending", result_complete=False, usage=None
    ) == {"status": "result_pending"}
    run, current_task, current_delivery = rows(domain, rid, task, delivery)
    assert run["budget"]["tokensUsed"] == 0
    assert current_task["reasonCode"] == "result_pending" and current_task["status"] == "running"
    assert not current_delivery.get("_terminalState")
    assert current_delivery["_tokenLimit"] == 100
    assert domain.project_run(
        delivery_id=delivery["deliveryId"],
        event_id="late-start",
        run_id="native-run",
        status="running",
    ) == {"status": "result_pending"}
    with pytest.raises(TeamsError) as blocked:
        domain.project_run(
            delivery_id=delivery["deliveryId"],
            event_id="wrong-port",
            run_id="native-run",
            status="succeeded",
        )
    assert blocked.value.code == "completion_projection_required"
    receipt = project(domain, delivery, evidence, candidate)
    run, current_task, current_delivery = rows(domain, rid, task, delivery)
    assert current_task["status"] == "awaiting_acceptance"
    assert current_task["attempts"][-1]["result"] == "最终报告"
    assert current_delivery["_terminalState"] == "succeeded"
    assert run["budget"]["tokensUsed"] == 15
    events = domain.events(OWNER, gid)
    assert project(domain, delivery, evidence, candidate) == receipt
    assert project(domain, delivery, evidence, candidate, "new-transport-key") == receipt
    assert (
        project(domain, delivery, pending, None, "late-pending", result_complete=False) == receipt
    )
    assert domain.events(OWNER, gid) == events
    assert rows(domain, rid, task, delivery)[0]["budget"]["tokensUsed"] == 15


def test_failure_rolls_back_candidate_terminal_usage_events_and_receipt(domain, monkeypatch):
    gid, rid, task, delivery, candidate, evidence = setup(domain)
    before_rows, before_events = rows(domain, rid, task, delivery), domain.events(OWNER, gid)
    original = domain._advance

    def fail_after_projection(tx, run):
        original(tx, run)
        raise RuntimeError("after candidate, terminal, budget and message writes")

    monkeypatch.setattr(domain, "_advance", fail_after_projection)
    with pytest.raises(RuntimeError, match="after candidate"):
        project(domain, delivery, evidence, candidate)
    assert rows(domain, rid, task, delivery) == before_rows
    assert domain.events(OWNER, gid) == before_events
    monkeypatch.setattr(domain, "_advance", original)
    project(domain, delivery, evidence, candidate)
    assert rows(domain, rid, task, delivery)[0]["budget"]["tokensUsed"] == 15


@pytest.mark.parametrize(
    "artifacts", [[], [{"artifactId": "verified-artifact", "sha256": "b" * 64}]]
)
def test_empty_result_is_missing_but_artifact_only_result_can_be_accepted(domain, artifacts):
    _, rid, task, delivery, _, evidence = setup(domain, acceptance="result")
    candidate = {"result": "", "artifacts": artifacts} if artifacts else None
    evidence["resultDigest"] = completion_result_digest(candidate) if candidate else None
    project(domain, delivery, evidence, candidate)
    run, current_task, current_delivery = rows(domain, rid, task, delivery)
    assert current_delivery["_terminalState"] == "succeeded"  # native fact is preserved
    if artifacts:
        assert current_task["status"] == "succeeded"
        assert current_task["attempts"][-1]["artifacts"] == artifacts
    else:
        assert current_task["status"] == "failed" and current_task["reasonCode"] == "result_missing"
        assert run["status"] == "needs_attention" and run["reason"] == "result_missing"
    assert run["budget"]["tokensUsed"] == 15


def test_exact_evidence_mode_digest_and_usage_cannot_change_after_completion(domain):
    gid, rid, task, delivery, candidate, evidence = setup(domain)
    for field, value in (("sessionId", "other"), ("runId", "other"), ("commandId", str(uuid4()))):
        with pytest.raises(TeamsError) as invalid:
            project(domain, delivery, {**evidence, field: value}, candidate)
        assert invalid.value.code == "run_identity_conflict"
    with pytest.raises(TeamsError) as wrong_result:
        project(domain, delivery, evidence, {"result": "changed", "artifacts": []})
    assert wrong_result.value.code == "result_digest_mismatch"
    project(domain, delivery, evidence, candidate)
    with pytest.raises(TeamsError) as changed_usage:
        project(domain, delivery, evidence, candidate, "changed-usage", usage={"totalTokens": 16})
    assert changed_usage.value.code == "completion_conflict"
    with pytest.raises(TeamsError) as contradictory_terminal:
        project(domain, delivery, {**evidence, "terminalSeq": 21}, candidate, "contradiction")
    assert contradictory_terminal.value.code == "terminal_evidence_conflict"
    assert rows(domain, rid, task, delivery)[0]["budget"]["tokensUsed"] == 15


def test_fenced_result_cannot_become_task_success_or_skip_acceptance(domain):
    _, rid, task, delivery, candidate, evidence = setup(domain, acceptance="result")
    with domain.store.transaction() as tx:
        current = tx.get("delivery", delivery["deliveryId"])
        current["_fenced"] = True
        tx.put("delivery", delivery["deliveryId"], current)
    project(domain, delivery, evidence, candidate)
    run, current_task, current_delivery = rows(domain, rid, task, delivery)
    assert current_task["status"] == "failed"
    assert current_delivery["_completionCandidate"] == candidate
    assert run["budget"]["tokensUsed"] == 15


def test_full_member_cannot_use_host_candidate_and_task_worker_cannot_use_model_tool(domain):
    gid, rid, task, delivery, candidate, evidence = setup(domain)
    actor = Actor(
        OWNER.tenant_id,
        "member",
        kind="member",
        group_id=gid,
        team_run_id=rid,
        member_id="worker",
        run_id="native-run",
        attempt_id=task["attempts"][-1]["attemptId"],
    )
    with pytest.raises(TeamsError) as wrong_port:
        domain.result_candidate(actor, task["taskId"], "model says done", "candidate")
    assert wrong_port.value.code == "candidate_mode_mismatch"
    with domain.store.transaction() as tx:
        current = tx.get("delivery", delivery["deliveryId"])
        current["_candidateMode"] = "team_tool"
        tx.put("delivery", delivery["deliveryId"], current)
    with pytest.raises(TeamsError) as wrong_mode:
        project(domain, delivery, evidence, candidate)
    assert wrong_mode.value.code == "candidate_mode_mismatch"


def test_concurrent_completion_settles_once_and_outer_transaction_can_roll_back(domain):
    gid, rid, task, delivery, candidate, evidence = setup(domain)
    before, events = rows(domain, rid, task, delivery), domain.events(OWNER, gid)
    with pytest.raises(RuntimeError, match="outer settlement"):
        with domain.store.transaction():
            project(domain, delivery, evidence, candidate)
            raise RuntimeError("outer settlement")
    assert rows(domain, rid, task, delivery) == before
    assert domain.events(OWNER, gid) == events
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(
            pool.map(lambda _: project(domain, delivery, evidence, candidate), range(6))
        )
    assert all(receipt == receipts[0] for receipt in receipts)
    assert rows(domain, rid, task, delivery)[0]["budget"]["tokensUsed"] == 15
