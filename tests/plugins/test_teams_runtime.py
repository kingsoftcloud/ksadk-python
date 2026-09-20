from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from ksadk.plugins.execution_host import ExecutionBarrier, ExecutionReceipt
from ksadk.plugins.teams.contracts import Actor, GroupCreateInput, MessageInput
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.runtime import TEAMS_PLUGIN_ID, TeamsRuntime
from ksadk.plugins.teams.store import TeamsStore

OWNER = Actor("tenant-a", "owner-a")
BINDING = {
    "bindingRef": "build-one",
    "kind": "local_build",
    "agentId": "writer",
    "buildId": "build-one",
    "providerRef": "harness",
    "capabilities": {
        "enqueue": True,
        "leader": True,
        "cancel": True,
        "restore": True,
        "steer": False,
        "interaction": True,
    },
}


class Host:
    def __init__(self):
        self.commands = {}
        self.submitted = []
        self.grants = {}
        self.admissions = {}
        self.grant_scopes = []
        self.cancelled = []
        self.lose_ack = False
        self.fail_barrier = False
        self.policies = []

    async def ensure_session(self, scope):
        assert scope.owner_subject == OWNER.subject

    async def submit(self, scope, **payload):
        key = payload["idempotency_key"]
        self.submitted.append(key)
        self.policies.append(payload["policy_context"])
        self.commands.setdefault(
            key,
            ExecutionReceipt(
                "accepted",
                command_id="command-" + key,
                message_id="message-" + key,
                run_id="run-" + key,
                run_status="running",
            ),
        )
        if self.lose_ack:
            raise ConnectionError("injected acknowledgement loss")
        return self.commands[key]

    async def lookup(self, scope, idempotency_key):
        return self.commands.get(idempotency_key, ExecutionReceipt("missing"))

    async def set_grant(self, scope, grant_id, state, idempotency_key):
        if self.fail_barrier:
            raise ConnectionError("injected barrier transport failure")
        self.grants[grant_id] = state
        self.grant_scopes.append((scope, grant_id, state))
        return ExecutionBarrier(state, 1)

    async def set_admission(self, scope, grant_id, allowed, idempotency_key):
        if self.fail_barrier:
            raise ConnectionError("injected admission barrier failure")
        self.admissions[grant_id] = allowed
        self.grants.setdefault(grant_id, "active")
        return ExecutionBarrier(self.grants[grant_id], 1, details={"admissionAllowed": allowed})

    async def cancel(self, scope, run_id, idempotency_key):
        self.cancelled.append(run_id)
        return ExecutionReceipt("accepted", run_id=run_id)


def setup(runtime):
    domain = runtime.require_domain()
    created = domain.create_group(
        OWNER,
        GroupCreateInput(
            name="测试协作",
            members=[{"memberId": "leader", "name": "协调员", "bindingRef": "build-one"}],
            leaderMemberId="leader",
            idempotencyKey="create",
        ),
        {"build-one": BINDING},
    )
    gid = created["group"]["groupId"]
    receipt = domain.send(
        OWNER,
        gid,
        MessageInput(
            parts=[{"kind": "text", "text": "完成方案"}], intent="start_goal", idempotencyKey="goal"
        ),
    )
    return domain, gid, receipt["teamRunId"]


def current(domain, gid):
    return domain.snapshot(OWNER, gid)["teamRuns"][-1]


def control(domain, gid, run_id, action, key):
    return domain.control(OWNER, gid, run_id, action, current(domain, gid)["revision"], key)


@pytest.mark.asyncio
async def test_lost_ack_restart_queries_original_command_without_resubmitting(tmp_path):
    host = Host()
    host.lose_ack = True
    path = tmp_path / "teams.sqlite"
    runtime = TeamsRuntime(path=path, authority_ref="local", host=host)
    await runtime.start(background=False)
    domain, gid, _ = setup(runtime)
    await runtime.tick()
    assert domain.snapshot(OWNER, gid)["deliveries"][0]["status"] == "uncertain"
    await runtime.close()
    restored = TeamsRuntime(path=path, authority_ref="local", host=host)
    await restored.start(background=False)
    try:
        await restored.tick()
        snapshot = restored.require_domain().snapshot(OWNER, gid)
        assert snapshot["deliveries"][0]["status"] == "accepted"
        assert snapshot["members"][0]["executionStatus"] == "running"
        assert len(host.submitted) == 1
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_authority_is_exclusive_and_released_on_close(tmp_path):
    options = {"path": tmp_path / "teams.sqlite", "authority_ref": "local", "host": Host()}
    first, second = TeamsRuntime(**options), TeamsRuntime(**options)
    assert not options["path"].exists()
    await first.start(background=False)
    with pytest.raises(TeamsError, match="已有运行"):
        await second.start(background=False)
    await first.close()
    await second.start(background=False)
    await second.close()


@pytest.mark.asyncio
async def test_stop_remains_requested_until_actual_execution_is_terminal(tmp_path):
    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        await runtime.tick()
        control(domain, gid, run_id, "stop", "stop")
        await runtime.tick()
        assert set(host.grants.values()) == {"revoked"}
        assert host.cancelled
        assert current(domain, gid)["status"] == "cancel_requested"
        key = host.submitted[0]
        host.commands[key] = replace(host.commands[key], run_status="cancelled")
        await runtime.tick()
        assert current(domain, gid)["status"] == "cancelled"
        assert len(host.submitted) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_resume_waits_for_all_barriers_and_stop_supersedes_stale_resume(tmp_path):
    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        control(domain, gid, run_id, "suspend_dispatch", "pause")
        await runtime.tick()
        control(domain, gid, run_id, "resume_dispatch", "resume")
        host.fail_barrier = True
        await runtime.tick()
        assert current(domain, gid)["dispatchSuspended"] is True
        assert not host.submitted
        control(domain, gid, run_id, "stop", "stop")
        host.fail_barrier = False
        await runtime.tick()
        assert current(domain, gid)["status"] == "cancelled"
        assert set(host.grants.values()) == {"revoked"}
        assert not host.submitted
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_successful_resume_admits_original_delivery_once(tmp_path):
    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        control(domain, gid, run_id, "suspend_dispatch", "pause")
        await runtime.tick()
        assert not host.submitted
        control(domain, gid, run_id, "resume_dispatch", "resume")
        await runtime.tick()
        assert current(domain, gid)["dispatchSuspended"] is False
        assert set(host.grants.values()) == {"active"}
        assert len(host.submitted) == 1
        await runtime.tick()
        assert len(host.submitted) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_token_reservations_bound_parallel_execution_and_reclaim_on_terminal(tmp_path):
    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain = runtime.require_domain()
        created = domain.create_group(
            OWNER,
            GroupCreateInput(
                name="预算",
                members=[
                    {"memberId": name, "name": name, "bindingRef": "build-one"}
                    for name in ("leader", "one", "two")
                ],
                leaderMemberId="leader",
                idempotencyKey="create",
            ),
            {"build-one": BINDING},
        )
        gid = created["group"]["groupId"]
        started = domain.send(
            OWNER,
            gid,
            MessageInput(
                parts=[{"kind": "text", "text": "goal"}],
                intent="start_goal",
                idempotencyKey="goal",
            ),
        )
        run_id = started["teamRunId"]
        with domain.store.transaction() as tx:
            run = tx.get("team_run", run_id)
            run["budget"].update(maxConcurrent=2, maxTokens=101)
            tx.put("team_run", run_id, run)
        for member in ("one", "two"):
            domain.send(
                OWNER,
                gid,
                MessageInput(
                    parts=[{"kind": "text", "text": "work"}],
                    mentions=[member],
                    intent="directed",
                    idempotencyKey="direct-" + member,
                ),
            )
        await runtime.tick()
        assert len(host.submitted) == 2
        assert [p["tokenLimit"] for p in host.policies] == [50, 50]
        await runtime.tick()
        assert len(host.submitted) == 2
        # Actual usage releases the unused reservation, never another live slot.
        key = host.submitted[0]
        host.commands[key] = replace(host.commands[key], run_status="succeeded", tokens=40)
        await runtime.tick()
        assert len(host.submitted) == 3
        assert host.policies[-1]["tokenLimit"] == 11
        assert current(domain, gid)["budget"]["tokensUsed"] == 40
        await runtime.tick()
        assert current(domain, gid)["budget"]["tokensUsed"] == 40
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_elapsed_budget_requests_stop_and_waits_for_actual_run(tmp_path):
    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        await runtime.tick()
        with domain.store.transaction() as tx:
            run = tx.get("team_run", run_id)
            run["activeDurationSeconds"] = run["budget"]["maxDurationSeconds"]
            tx.put("team_run", run_id, run)
        await runtime.tick()
        assert current(domain, gid)["status"] == "cancel_requested"
        assert current(domain, gid)["reason"] == "duration_budget_exhausted"
        assert host.cancelled
        key = host.submitted[0]
        host.commands[key] = replace(host.commands[key], run_status="cancelled")
        await runtime.tick()
        assert current(domain, gid)["status"] == "cancelled"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_waiting_approval_and_final_acceptance_do_not_use_active_duration(
    tmp_path, monkeypatch
):
    from importlib import import_module

    domain_module = import_module("ksadk.plugins.teams.domain")
    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        delivery = domain.snapshot(OWNER, gid)["deliveries"][0]
        monkeypatch.setattr(domain_module, "now", lambda: "2026-09-12T00:00:00+00:00")
        domain.project_run(
            delivery_id=delivery["deliveryId"], event_id="started", run_id="real", status="running"
        )
        monkeypatch.setattr(domain_module, "now", lambda: "2026-09-12T00:00:10+00:00")
        domain.project_run(
            delivery_id=delivery["deliveryId"],
            event_id="waiting",
            run_id="real",
            status="awaiting_approval",
        )
        monkeypatch.setattr(domain_module, "now", lambda: "2026-09-13T00:00:00+00:00")
        runtime._watchdog(domain)
        run = current(domain, gid)
        assert run["activeDurationSeconds"] == 10
        assert run["status"] != "cancel_requested"
        published = [
            event["payload"]["teamRun"]
            for event in domain.events(OWNER, gid)
            if event["type"] == "team_run.updated"
        ]
        assert published[-1]["activeDurationSeconds"] == run["activeDurationSeconds"]
        assert published[-1]["revision"] == run["revision"]
        with domain.store.transaction() as tx:
            stored = tx.get("team_run", run_id)
            stored.update(status="awaiting_acceptance")
            stored["budget"]["tokensUsed"] = stored["budget"]["maxTokens"]
            tx.put("team_run", run_id, stored)
        runtime._watchdog(domain)
        assert current(domain, gid)["status"] == "awaiting_acceptance"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_preflight_failure_is_rejected_and_offline_node_stays_pending(tmp_path):
    class PreflightHost(Host):
        error_code = "execution_node_offline"

        async def ensure_session(self, scope):
            raise TeamsError(self.error_code, "safe fixture", status=503)

    host = PreflightHost()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, _ = setup(runtime)
        await runtime.tick()
        delivery = domain.snapshot(OWNER, gid)["deliveries"][0]
        assert delivery["status"] == "pending" and delivery["reason"] == "waiting_for_node"
        assert current(domain, gid)["activeDurationSeconds"] == 0
        assert host.submitted == []
        host.error_code = "BUILD_UNAVAILABLE"
        with domain.store.transaction() as tx:
            stored = tx.get("delivery", delivery["deliveryId"])
            stored["_nextRetryAt"] = 0
            tx.put("delivery", stored["deliveryId"], stored)
        await runtime.tick()
        rejected = domain.snapshot(OWNER, gid)["deliveries"][0]
        assert rejected["status"] == "rejected" and rejected["reason"] == "BUILD_UNAVAILABLE"
        assert host.submitted == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_close_is_bounded_and_keeps_authority_until_old_writer_stops(tmp_path):
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=Host())
    await runtime.start(background=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stuck_writer():
        async with runtime._tick_lock:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    writer = runtime._task = asyncio.create_task(stuck_writer())
    await entered.wait()
    await asyncio.wait_for(runtime.close(timeout=0.01), timeout=0.2)
    assert runtime.last_error == "teams_shutdown_timeout"
    assert runtime.domain is not None and runtime._authority_file is not None
    competing = TeamsRuntime(path=runtime.path, authority_ref="local", host=Host())
    with pytest.raises(TeamsError, match="已有运行"):
        await competing.start(background=False)
    release.set()
    await writer
    await runtime.close(timeout=0.1)
    assert runtime.domain is None
    await competing.start(background=False)
    await competing.close()


@pytest.mark.asyncio
async def test_same_member_in_parallel_runs_uses_distinct_sessions_and_concurrency(tmp_path):
    class SessionHost(Host):
        def __init__(self):
            super().__init__()
            self.sessions = []

        async def submit(self, scope, **payload):
            self.sessions.append(scope.session_id)
            return await super().submit(scope, **payload)

    host = SessionHost()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, first = setup(runtime)
        second = domain.send(
            OWNER,
            gid,
            MessageInput(
                parts=[{"kind": "text", "text": "另一个任务"}],
                intent="start_goal",
                idempotencyKey="parallel",
            ),
        )["teamRunId"]
        await runtime.tick()
        assert len(set(host.sessions)) == 2
        assert len(host.submitted) == 2
        await runtime.tick()
        members = domain.snapshot(OWNER, gid)["runMembers"]
        assert {m["teamRunId"] for m in members if m["executionStatus"] == "running"} == {
            first,
            second,
        }
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_fenced_delivery_reconciles_original_scope_and_keeps_budget_reservation(tmp_path):
    class RecordingHost(Host):
        def __init__(self):
            super().__init__()
            self.lookups = []

        async def lookup(self, scope, idempotency_key):
            self.lookups.append(scope.session_id)
            return await super().lookup(scope, idempotency_key)

    host = RecordingHost()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        await runtime.tick()
        with domain.store.transaction() as tx:
            old = tx.list("delivery", gid)[0]
            old_session = old["_sessionId"]
            old.update(_fenced=True, _tokenLimit=40)
            tx.put("delivery", old["deliveryId"], old)
            member = domain.run_member(tx, run_id, "leader")
            member["sessionId"] = "new-session"
            tx.put("run_member", member["runMemberId"], member)
            run = tx.get("team_run", run_id)
            run["_roster"][0] = member
            run["budget"]["maxTokens"] = 100
            tx.put("team_run", run_id, run)
        domain.send(
            OWNER,
            gid,
            MessageInput(
                parts=[{"kind": "text", "text": "继续已确认的工作"}],
                intent="followup",
                teamRunId=run_id,
                idempotencyKey="takeover",
            ),
        )
        await runtime.tick()
        assert host.lookups[0] == old_session
        assert len(host.submitted) == 2
        assert host.policies[-1]["tokenLimit"] == 60
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_source_refs_project_children_once_and_survive_replay(tmp_path):
    class SourceHost(Host):
        events = []

        async def source_events(self, scope, *, after, limit):
            items = [event for event in self.events if event["seq"] > after][:limit]
            return {"items": items, "cursor": items[-1]["seq"] if items else after}

    host = SourceHost()
    path = tmp_path / "teams.sqlite"
    runtime = TeamsRuntime(path=path, authority_ref="local", host=host)
    await runtime.start(background=False)
    domain, gid, run_id = setup(runtime)
    try:
        await runtime.tick()
        native_run = host.commands[host.submitted[0]].run_id

        def event(seq, event_type, child, parent, *, item_id=None):
            return {
                "event_id": f"evt-{seq}",
                "seq": seq,
                "family": "runtime",
                "run_id": native_run,
                "payload": {
                    **({"item_id": item_id} if item_id else {}),
                    "source": {
                        "native_run_id": child,
                        "metadata": {
                            "native_event_type": event_type,
                            "parent_run_id": parent,
                            "agent_id": "reviewer",
                        },
                    },
                },
            }

        host.events = [
            event(1, "run.started", "child", native_run),
            event(2, "run.started", "grandchild", "child", item_id="status-2"),
            event(3, "run.completed", "child", native_run),
        ]
        # A status-like text cannot invent child execution facts.
        host.events.append(
            {
                "seq": 4,
                "event_id": "fake",
                "family": "runtime",
                "run_id": native_run,
                "payload": {"text": "child completed"},
            }
        )
        await runtime.tick()
        execution = domain.execution(OWNER, gid, run_id)
        children = [node for node in execution["nodes"] if node["kind"] == "child_invocation"]
        assert len(children) == 2
        assert sorted(child["status"] for child in children) == ["running", "succeeded"]
        assert all(child["source"]["runId"] == native_run for child in children)
        assert all(
            "itemId" not in child["source"] or child["source"]["itemId"] == "status-2"
            for child in children
        )
        assert len([edge for edge in execution["edges"] if edge["kind"] == "invocation"]) == 2
        watermark = execution["watermark"]
        await runtime.close()
        await runtime.start(background=False)
        await runtime.tick()
        replay = runtime.require_domain().execution(OWNER, gid, run_id)
        assert replay["watermark"] == watermark
        assert replay["nodes"] == execution["nodes"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_pause_blocks_admission_but_running_completion_and_budget_continue(tmp_path):
    from datetime import datetime, timedelta, timezone

    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        await runtime.tick()
        await runtime.tick()  # reconcile the canonical running fact
        with domain.store.transaction() as tx:
            run = tx.get("team_run", run_id)
            run["_activeSince"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
            tx.put("team_run", run_id, run)
        control(domain, gid, run_id, "suspend_dispatch", "pause")
        await runtime.tick()
        assert current(domain, gid)["dispatchSuspended"] is True
        assert current(domain, gid)["activeDurationSeconds"] >= 2
        assert set(host.admissions.values()) == {False}
        assert set(host.grants.values()) == {"active"}
        assert not host.cancelled
        key = host.submitted[0]
        host.commands[key] = replace(
            host.commands[key], run_status="succeeded", output="done", tokens=11
        )
        await runtime.tick()
        assert current(domain, gid)["budget"]["tokensUsed"] == 11
        assert current(domain, gid)["dispatchSuspended"] is True
        assert set(host.grants.values()) == {"active"}
        # The original member grant can be reused after completion. Resume
        # must reopen its admission even though the old delivery is terminal.
        control(domain, gid, run_id, "resume_dispatch", "resume-after-completion")
        await runtime.tick()
        assert set(host.admissions.values()) == {True}
        assert current(domain, gid)["dispatchSuspended"] is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_missing_admission_capability_does_not_pretend_pause_completed(tmp_path):
    host = Host()
    host.set_admission = None
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        control(domain, gid, run_id, "suspend_dispatch", "pause")
        await runtime.tick()
        with domain.store.transaction() as tx:
            pending = tx.list("control", gid)[0]
        assert pending["status"] == "pending"
        assert pending["reason"] == "admission_barrier_unsupported"
        assert not host.grants and not host.submitted
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_legacy_host_terminal_without_evidence_cannot_create_task_worker_result(tmp_path):
    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        await runtime.tick()
        key = host.submitted[0]
        with domain.store.transaction() as tx:
            delivery = tx.get("delivery", key)
            delivery["_candidateMode"] = "canonical_terminal"
            tx.put("delivery", key, delivery)
        host.commands[key] = replace(
            host.commands[key], run_status="succeeded", output="unproven", tokens=40
        )
        await runtime.tick()
        with domain.store.transaction() as tx:
            pending = tx.get("delivery", key)
        assert pending["reason"] == "completion_evidence_required"
        assert pending["resultState"] == "pending"
        assert not pending.get("_terminalState")
        assert current(domain, gid)["budget"]["tokensUsed"] == 0
        watermark = domain.snapshot(OWNER, gid)["watermark"]
        await runtime.tick()
        assert domain.snapshot(OWNER, gid)["watermark"] == watermark
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_stop_freezes_exact_attempt_grants_and_original_cancel_scope(tmp_path):
    from ksadk.plugins.teams.contracts import TaskCreateInput

    host = Host()
    runtime = TeamsRuntime(path=tmp_path / "teams.sqlite", authority_ref="local", host=host)
    await runtime.start(background=False)
    try:
        domain, gid, run_id = setup(runtime)
        await runtime.tick()
        domain.create_task(
            OWNER, gid, run_id, TaskCreateInput(title="next", ownerMemberId="leader"), "task"
        )
        with domain.store.transaction() as tx:
            deliveries = tx.list("delivery", gid)
            original = deliveries[0]
            queued = deliveries[1]
            queued["_grantId"] = "attempt-specific-grant"
            tx.put("delivery", queued["deliveryId"], queued)
        control(domain, gid, run_id, "stop", "stop")
        await runtime.tick()
        with domain.store.transaction() as tx:
            saved = tx.list("control", gid)[0]
        assert {target["grantId"] for target in saved["targets"]} == {
            original["_grantId"],
            "attempt-specific-grant",
        }
        assert set(host.grants) == {original["_grantId"], "attempt-specific-grant"}
        assert all(state == "revoked" for _, _, state in host.grant_scopes)
        intent = saved["cancelRequests"][original["deliveryId"]]
        assert intent["runId"] == original["runId"]
        assert intent["sessionId"] == original["_sessionId"]
        assert host.cancelled == [original["runId"]]
        assert current(domain, gid)["status"] == "cancel_requested"
        key = host.submitted[0]
        host.commands[key] = replace(host.commands[key], run_status="interrupted")
        await runtime.tick()
        assert current(domain, gid)["status"] == "cancelled"
        with domain.store.transaction() as tx:
            terminal = tx.get("delivery", original["deliveryId"])
        assert terminal["_terminalState"] == "interrupted"
        assert len(host.submitted) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_changed_compatible_plugin_artifact_is_backed_up_and_migrated(tmp_path):
    options = {"path": tmp_path / "teams.sqlite", "authority_ref": "local", "host": Host()}
    first = TeamsRuntime(**options, plugin_digest="sha256:original")
    await first.start(background=False)
    domain, gid, _ = setup(first)
    before = domain.snapshot(OWNER, gid)
    await first.close()
    changed = TeamsRuntime(**options, plugin_digest="sha256:different")
    await changed.start(background=False)
    try:
        assert changed.require_domain().snapshot(OWNER, gid) == before
        backups = list(tmp_path.glob("teams.sqlite.pre-artifact-*.bak"))
        assert len(backups) == 1
        backup = TeamsStore(backups[0])
        try:
            with backup.transaction() as tx:
                installed = tx.get("installation", TEAMS_PLUGIN_ID)
            assert installed["pluginDigest"] == "sha256:original"
        finally:
            backup.close()
        with changed.require_domain().store.transaction() as tx:
            installed = tx.get("installation", TEAMS_PLUGIN_ID)
        assert installed["pluginDigest"] == "sha256:different"
        assert installed["previousPluginDigest"] == "sha256:original"
    finally:
        await changed.close()


@pytest.mark.asyncio
async def test_changed_incompatible_plugin_artifact_still_fails_closed(tmp_path):
    options = {"path": tmp_path / "teams.sqlite", "authority_ref": "local", "host": Host()}
    first = TeamsRuntime(**options, plugin_digest="sha256:original")
    await first.start(background=False)
    domain, gid, _ = setup(first)
    before = domain.snapshot(OWNER, gid)
    with domain.store.transaction() as tx:
        installed = tx.get("installation", TEAMS_PLUGIN_ID)
        tx.put("installation", TEAMS_PLUGIN_ID, {**installed, "apiVersion": "teams/v2"})
    await first.close()

    changed = TeamsRuntime(**options, plugin_digest="sha256:different")
    with pytest.raises(TeamsError) as error:
        await changed.start(background=False)
    assert error.value.code == "artifact_migration_required"
    assert changed.domain is None
    assert not list(tmp_path.glob("teams.sqlite.pre-artifact-*.bak"))

    original = TeamsRuntime(**options, plugin_digest="sha256:original")
    await original.start(background=False)
    try:
        assert original.require_domain().snapshot(OWNER, gid) == before
    finally:
        await original.close()
