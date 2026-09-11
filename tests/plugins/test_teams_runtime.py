from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ksadk.plugins.execution_host import ExecutionBarrier, ExecutionReceipt
from ksadk.plugins.teams.contracts import Actor, GroupCreateInput, MessageInput
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.runtime import TeamsRuntime

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
        return ExecutionBarrier(state, 1)

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
            run["createdAt"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
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
async def test_changed_plugin_artifact_fails_closed_and_original_version_reopens(tmp_path):
    options = {"path": tmp_path / "teams.sqlite", "authority_ref": "local", "host": Host()}
    first = TeamsRuntime(**options, plugin_digest="sha256:original")
    await first.start(background=False)
    domain, gid, _ = setup(first)
    before = domain.snapshot(OWNER, gid)
    await first.close()
    changed = TeamsRuntime(**options, plugin_digest="sha256:different")
    with pytest.raises(TeamsError) as error:
        await changed.start(background=False)
    assert error.value.code == "artifact_migration_required"
    assert changed.domain is None
    # Failed startup releases both database and authority ownership.
    await first.start(background=False)
    try:
        assert first.require_domain().snapshot(OWNER, gid) == before
    finally:
        await first.close()
