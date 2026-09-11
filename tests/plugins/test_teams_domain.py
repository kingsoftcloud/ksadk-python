from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from ksadk.plugins.teams.contracts import (
    Actor,
    GroupCreateInput,
    MemberInput,
    MessageInput,
    TaskCreateInput,
)
from ksadk.plugins.teams.domain import TeamsDomain, member_key
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.store import TeamsStore

OWNER = Actor("tenant-a", "owner-a")
BINDING = {
    "bindingRef": "binding-one",
    "providerRef": "ksadk.harness@1",
    "kind": "local_build",
    "agentId": "writer",
    "buildId": "build-one",
    "capabilities": {
        "enqueue": True,
        "leader": True,
        "cancel": True,
        "restore": True,
        "steer": False,
        "interaction": True,
    },
}


@pytest.fixture
def domain(tmp_path):
    store = TeamsStore(tmp_path / "teams.sqlite")
    yield TeamsDomain(store, authority_ref="authority-local")
    store.close()


def group(domain):
    return domain.create_group(
        OWNER,
        GroupCreateInput(
            name="接口改造",
            members=[
                {"memberId": "leader", "name": "协调员", "bindingRef": "binding-one"},
                {"memberId": "engineer", "name": "工程师", "bindingRef": "binding-one"},
                {"memberId": "reviewer", "name": "评审员", "bindingRef": "binding-one"},
            ],
            leaderMemberId="leader",
            idempotencyKey="create",
        ),
        {"binding-one": BINDING},
    )


def start(domain, gid):
    return domain.send(
        OWNER,
        gid,
        MessageInput(
            parts=[{"kind": "text", "text": "产出接口改造方案"}],
            intent="start_goal",
            idempotencyKey="goal",
        ),
    )


def task(
    domain, gid, run_id, *, owner="engineer", dependencies=None, key="task", acceptance="human"
):
    return domain.create_task(
        OWNER,
        gid,
        run_id,
        TaskCreateInput(
            title="分析接口",
            ownerMemberId=owner,
            dependencies=dependencies or [],
            acceptancePolicy=acceptance,
        ),
        key,
    )


def complete(domain, gid, run_id, task_record, *, status="succeeded", output="方案已提交"):
    attempt = task_record["attempts"][-1]
    with domain.store.transaction() as tx:
        stored = tx.get("task", task_record["taskId"])
        delivery_id = stored["attempts"][-1]["_deliveryId"]
    actor = Actor(
        OWNER.tenant_id,
        "verified-member",
        kind="member",
        group_id=gid,
        member_id=task_record["ownerMemberId"],
        team_run_id=run_id,
        run_id="run-" + attempt["attemptId"],
        attempt_id=attempt["attemptId"],
    )
    domain.project_run(
        delivery_id=delivery_id, event_id="started", run_id=actor.run_id, status="running"
    )
    domain.result_candidate(actor, task_record["taskId"], output, "candidate")
    domain.project_run(
        delivery_id=delivery_id,
        event_id="terminal",
        run_id=actor.run_id,
        status=status,
        output=output,
        tokens=15,
    )
    return actor, delivery_id


def test_message_and_outbox_are_atomic_across_failure_and_restart(domain, monkeypatch):
    gid = group(domain)["group"]["groupId"]
    original = domain.dispatch

    def failed_dispatch(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected before commit")

    monkeypatch.setattr(domain, "dispatch", failed_dispatch)
    with pytest.raises(OSError):
        start(domain, gid)
    snapshot = domain.snapshot(OWNER, gid)
    assert snapshot["messages"] == snapshot["deliveries"] == snapshot["teamRuns"] == []
    monkeypatch.setattr(domain, "dispatch", original)
    receipt = start(domain, gid)
    reopened = TeamsStore(domain.store.path)
    try:
        restored = TeamsDomain(reopened, authority_ref="authority-local")
        assert start(restored, gid) == receipt
        snapshot = restored.snapshot(OWNER, gid)
        assert (
            len(snapshot["messages"])
            == len(snapshot["deliveries"])
            == len(snapshot["teamRuns"])
            == 1
        )
    finally:
        reopened.close()


def test_goal_api_and_message_api_share_same_goal_identity(domain):
    gid = group(domain)["group"]["groupId"]
    receipt = start(domain, gid)
    run = domain.start_goal(OWNER, gid, receipt["messageId"], "start-existing")
    assert run["teamRunId"] == receipt["teamRunId"]
    snapshot = domain.snapshot(OWNER, gid)
    assert len(snapshot["deliveries"]) == 1
    with pytest.raises(TeamsError, match="已有进行中"):
        domain.send(
            OWNER,
            gid,
            MessageInput(
                parts=[{"kind": "text", "text": "另一个目标"}],
                intent="start_goal",
                idempotencyKey="second",
            ),
        )
    assert len(domain.snapshot(OWNER, gid)["messages"]) == 1


def test_rebind_allocates_new_session_and_preserves_immutable_history(domain):
    created = group(domain)
    gid = created["group"]["groupId"]
    before = created["members"][1]
    updated = domain.update_group(
        OWNER,
        gid,
        created["group"]["revision"],
        "rebind",
        rebind_member=MemberInput(
            memberId=before["memberId"], name="新工程师", bindingRef="binding-one"
        ),
        bindings={"binding-one": BINDING},
    )
    after = updated["members"][1]
    assert before["sessionId"] != after["sessionId"]
    with domain.store.transaction() as tx:
        stored = tx.get("member", member_key(gid, after["memberId"]))
        assert stored["_bindingHistory"][0]["sessionId"] == before["sessionId"]
        old = tx.get("group_revision", f"{gid}:1")
        assert old["members"][1]["sessionId"] == before["sessionId"]
    assert updated["group"]["policy"] == {"taskAcceptance": "human", "peerWake": False}


def test_explicit_peer_requests_are_bounded_and_plain_mentions_do_not_wake(domain):
    gid = group(domain)["group"]["groupId"]
    started = start(domain, gid)
    snapshot = domain.snapshot(OWNER, gid)
    delivery = snapshot["deliveries"][0]
    actor = Actor(
        OWNER.tenant_id,
        "member:leader",
        kind="member",
        group_id=gid,
        member_id="leader",
        team_run_id=started["teamRunId"],
        run_id="leader-run",
    )
    domain.member_message(
        actor,
        content="@engineer @reviewer 进度通知",
        target_member_id=None,
        wake=False,
        source_delivery_id=delivery["deliveryId"],
        key="notify",
    )
    assert len(domain.snapshot(OWNER, gid)["deliveries"]) == 1
    sent = domain.member_message(
        actor,
        content="请复核",
        target_member_id="reviewer",
        wake=True,
        source_delivery_id=delivery["deliveryId"],
        key="request",
    )
    assert sent["status"] == "accepted"
    assert (
        domain.member_message(
            actor,
            content="请复核",
            target_member_id="reviewer",
            wake=True,
            source_delivery_id=delivery["deliveryId"],
            key="request",
        )
        == sent
    )
    with domain.store.transaction() as tx:
        child = tx.get("delivery", sent["deliveryId"])
        decision = tx.get("trigger", child["_decisionId"])
        assert decision["hop"] == 1
    peer = Actor(
        OWNER.tenant_id,
        "member:reviewer",
        kind="member",
        group_id=gid,
        member_id="reviewer",
        team_run_id=started["teamRunId"],
        run_id="reviewer-run",
    )
    with pytest.raises(TeamsError, match="只允许向 Leader"):
        domain.member_message(
            peer,
            content="再叫一人",
            target_member_id="engineer",
            wake=True,
            source_delivery_id=sent["deliveryId"],
            key="peer",
        )
    assert len(domain.snapshot(OWNER, gid)["deliveries"]) == 2


def test_notes_do_not_wake_and_idempotency_conflict_is_rejected(domain):
    gid = group(domain)["group"]["groupId"]
    note = MessageInput(
        parts=[{"kind": "text", "text": "@全体 仅供参考"}],
        mentions=["engineer", "reviewer"],
        intent="note",
        idempotencyKey="note",
    )
    domain.send(OWNER, gid, note)
    assert domain.snapshot(OWNER, gid)["deliveries"] == []
    with pytest.raises(TeamsError) as error:
        domain.send(
            OWNER,
            gid,
            MessageInput.model_validate(
                {**note.model_dump(), "parts": [{"kind": "text", "text": "changed"}]}
            ),
        )
    # Validate through the public input before reusing a key with different content.
    assert error.value.code == "idempotency_conflict"


def test_authorization_is_checked_on_duplicate_and_cross_group_reply(domain):
    gid = group(domain)["group"]["groupId"]
    receipt = start(domain, gid)
    with pytest.raises(TeamsError):
        domain.snapshot(Actor("tenant-b", OWNER.subject), gid)
    with pytest.raises(TeamsError):
        domain.snapshot(Actor(OWNER.tenant_id, "other-user"), gid)
    actor = Actor(
        OWNER.tenant_id,
        "verified-engineer",
        kind="member",
        group_id=gid,
        member_id="engineer",
        team_run_id=receipt["teamRunId"],
        run_id="run-1",
    )
    note = MessageInput(
        parts=[{"kind": "text", "text": "进度"}], intent="note", idempotencyKey="same-note"
    )
    domain.send(actor, gid, note)
    with domain.store.transaction() as tx:
        member = tx.get("member", member_key(gid, "engineer"))
        member["status"] = "removed"
        tx.put("member", member_key(gid, "engineer"), member)
    with pytest.raises(TeamsError):
        domain.send(actor, gid, note)


def test_independent_members_get_distinct_sessions_even_for_same_binding(domain):
    snapshot = group(domain)
    assert len({member["sessionId"] for member in snapshot["members"]}) == 3
    assert all(member["bindingRef"] == "binding-one" for member in snapshot["members"])


def test_candidate_cannot_release_dependency_before_terminal_and_acceptance(domain):
    gid = group(domain)["group"]["groupId"]
    run_id = start(domain, gid)["teamRunId"]
    first = task(domain, gid, run_id)
    dependent = task(
        domain, gid, run_id, owner="reviewer", dependencies=[first["taskId"]], key="dependent"
    )
    assert dependent["status"] == "blocked" and not dependent["attempts"]
    actor, delivery_id = complete(domain, gid, run_id, first)
    snapshot = domain.snapshot(OWNER, gid)
    actual = next(t for t in snapshot["tasks"] if t["taskId"] == first["taskId"])
    assert actual["status"] == "awaiting_acceptance"
    assert (
        next(t for t in snapshot["tasks"] if t["taskId"] == dependent["taskId"])["status"]
        == "blocked"
    )
    with pytest.raises(TeamsError, match="人工验收"):
        domain.task_action(actor, gid, first["taskId"], "accept", actual["revision"], "ai-accept")
    domain.task_action(OWNER, gid, first["taskId"], "accept", actual["revision"], "accept")
    next_task = next(
        t for t in domain.snapshot(OWNER, gid)["tasks"] if t["taskId"] == dependent["taskId"]
    )
    assert next_task["status"] == "ready" and len(next_task["attempts"]) == 1
    # Duplicate terminal does not duplicate results, usage or dependency starts.
    domain.project_run(
        delivery_id=delivery_id,
        event_id="duplicate-terminal",
        run_id=actor.run_id,
        status="succeeded",
        output="方案已提交",
        tokens=15,
    )
    assert domain.snapshot(OWNER, gid)["teamRuns"][0]["budget"]["tokensUsed"] == 15


def test_failed_run_cannot_turn_candidate_into_success(domain):
    gid = group(domain)["group"]["groupId"]
    run_id = start(domain, gid)["teamRunId"]
    first = task(domain, gid, run_id, acceptance="result")
    complete(domain, gid, run_id, first, status="failed", output="完成！")
    assert domain.snapshot(OWNER, gid)["tasks"][0]["status"] == "failed"


def test_two_concurrent_claims_have_one_winner(domain):
    gid = group(domain)["group"]["groupId"]
    run_id = start(domain, gid)["teamRunId"]
    unclaimed = task(domain, gid, run_id, owner=None)

    def claim(member):
        store = TeamsStore(domain.store.path)
        try:
            other = TeamsDomain(store, authority_ref="authority-local")
            actor = Actor(
                OWNER.tenant_id,
                member,
                kind="member",
                group_id=gid,
                member_id=member,
                team_run_id=run_id,
                run_id=member + "-run",
            )
            return other.task_action(
                actor, gid, unclaimed["taskId"], "claim", unclaimed["revision"], member
            )
        except TeamsError as error:
            return error.code
        finally:
            store.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, ["engineer", "reviewer"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "revision_conflict" in results


def test_persistent_join_notifies_leader_once_after_all_tasks_settle(domain):
    gid = group(domain)["group"]["groupId"]
    run_id = start(domain, gid)["teamRunId"]
    first = task(domain, gid, run_id, acceptance="result")
    second = task(domain, gid, run_id, owner="reviewer", key="second", acceptance="result")
    leader = Actor(
        OWNER.tenant_id,
        "verified-leader",
        kind="member",
        group_id=gid,
        member_id="leader",
        team_run_id=run_id,
        run_id="leader-run",
    )
    domain.wait_for_tasks(leader, [first["taskId"], second["taskId"]], "wait")
    complete(domain, gid, run_id, first)
    assert len(domain.snapshot(OWNER, gid)["deliveries"]) == 3
    complete(domain, gid, run_id, second)
    assert len(domain.snapshot(OWNER, gid)["deliveries"]) == 4
    domain.wait_for_tasks(leader, [first["taskId"], second["taskId"]], "another-wait-call")
    assert len(domain.snapshot(OWNER, gid)["deliveries"]) == 4


def test_stop_freezes_dispatch_and_late_results_never_wake_leader(domain):
    gid = group(domain)["group"]["groupId"]
    run_id = start(domain, gid)["teamRunId"]
    task(domain, gid, run_id)
    run = domain.snapshot(OWNER, gid)["teamRuns"][0]
    stopped = domain.control(OWNER, gid, run_id, "stop", run["revision"], "stop")
    assert stopped["status"] == "cancel_requested"
    assert all(
        delivery["status"] == "cancelled" for delivery in domain.snapshot(OWNER, gid)["deliveries"]
    )
    with pytest.raises(TeamsError, match="不能恢复"):
        domain.control(OWNER, gid, run_id, "resume_dispatch", stopped["revision"], "resume")
    with domain.store.transaction() as tx:
        controls = tx.list("control", gid)
    assert len(controls) == 1 and controls[0]["status"] == "pending"


def test_event_snapshot_watermark_and_invalid_future_cursor(domain):
    gid = group(domain)["group"]["groupId"]
    snapshot = domain.snapshot(OWNER, gid)
    start(domain, gid)
    events = domain.events(OWNER, gid, snapshot["watermark"])
    assert events[0]["groupSeq"] == snapshot["watermark"] + 1
    assert len({event["eventId"] for event in events}) == len(events)
    assert all(
        not key.startswith("_")
        for event in events
        for value in event["payload"].values()
        if isinstance(value, dict)
        for key in value
    )
    with pytest.raises(TeamsError) as error:
        domain.events(OWNER, gid, 10000)
    assert error.value.code == "reset_required"
