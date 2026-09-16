from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from ksadk.plugins.execution_host import ExecutionReceipt
from ksadk.plugins.teams.api import create_router
from ksadk.plugins.teams.application import TeamsApplication
from ksadk.plugins.teams.contracts import Actor
from ksadk.plugins.teams.errors import TeamsError
from ksadk.studio.workspace_plugins import WorkspacePlugin, WorkspacePluginRegistry

OWNER = Actor("local-studio", "local-user")


class Host:
    def register_plugin(self, *args, **kwargs):
        return lambda: None

    async def describe(self, scope):
        return {
            "bindingRef": scope.binding_ref,
            "kind": "local_build",
            "agentId": "agent-one",
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


@pytest_asyncio.fixture
async def client(tmp_path):
    feature = TeamsApplication(
        path=tmp_path / "teams.sqlite",
        authority_ref="local-authority",
        host=Host(),
        actor=lambda: OWNER,
        list_build_ids=lambda: ["build-one"],
        workspace_root=tmp_path / "workspaces",
    )
    await feature.runtime.start(background=False)
    registry = WorkspacePluginRegistry()
    enabled = False

    async def enable():
        nonlocal enabled
        registry.contribute_routes("teams", create_router(feature))
        enabled = True

    async def disable():
        nonlocal enabled
        registry.remove_routes("teams")
        enabled = False

    registry.register(
        WorkspacePlugin("teams", "teams.ksadk.io/v1", enable, disable, lambda: {"enabled": enabled})
    )
    app = FastAPI()
    app.mount("/api/v1", registry.api)
    await enable()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http, feature
    finally:
        await feature.close()


async def create(http):
    response = await http.post(
        "/api/v1/groups",
        json={
            "name": "接口评审",
            "leaderMemberId": "leader",
            "idempotencyKey": "create",
            "members": [
                {"memberId": "leader", "name": "协调员", "bindingRef": "local-build:build-one"},
                {"memberId": "reviewer", "name": "评审员", "bindingRef": "local-build:build-one"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_api_start_goal_is_atomic_and_retries_return_same_round(client):
    http, feature = client
    group = await create(http)
    gid = group["group"]["groupId"]
    request = {
        "parts": [{"kind": "text", "text": "分析接口"}],
        "mentions": [],
        "intent": "start_goal",
        "idempotencyKey": "goal",
    }
    first = await http.post(f"/api/v1/groups/{gid}/messages", json=request)
    assert first.status_code == 202
    assert (await http.post(f"/api/v1/groups/{gid}/messages", json=request)).json() == first.json()
    snapshot = (await http.get(f"/api/v1/groups/{gid}")).json()
    assert len(snapshot["teamRuns"]) == len(snapshot["deliveries"]) == 1
    result = await http.post(
        f"/api/v1/groups/{gid}/team-runs",
        json={"goalMessageId": first.json()["messageId"], "idempotencyKey": "start-existing"},
    )
    assert result.json()["teamRunId"] == snapshot["teamRuns"][0]["teamRunId"]
    assert (await http.get("/api/v1/groups/bindings")).json()["items"]
    assert feature.domain.snapshot(OWNER, gid)["group"]["ownerSubject"] == OWNER.subject


@pytest.mark.asyncio
async def test_api_rejects_forged_principal_and_cross_member_observer(client):
    http, feature = client
    group = await create(http)
    gid = group["group"]["groupId"]
    forged = await http.post(
        f"/api/v1/groups/{gid}/messages",
        json={
            "parts": [{"kind": "text", "text": "伪造"}],
            "intent": "note",
            "mentions": [],
            "idempotencyKey": "forged",
            "senderPrincipal": "leader",
            "ownerSubject": "intruder",
        },
    )
    assert forged.status_code == 422
    leader, reviewer = group["members"]
    response = await http.get(
        f"/api/v1/groups/{gid}/members/{leader['memberId']}/conversation",
        params={"sessionId": reviewer["sessionId"], "runId": "unknown"},
    )
    assert response.status_code == 403
    assert feature.domain.snapshot(OWNER, gid)["messages"] == []


@pytest.mark.asyncio
async def test_expired_cursor_emits_explicit_reset_and_disabled_plugin_loses_routes(client):
    http, feature = client
    group = await create(http)
    gid = group["group"]["groupId"]
    reset = await http.get(f"/api/v1/groups/{gid}/events?after=999999")
    assert reset.status_code == 200
    assert "event: reset_required" in reset.text
    disabled = await http.post("/api/v1/plugins/teams/lifecycle", json={"enabled": False})
    assert disabled.json()["enabled"] is False
    assert (await http.get(f"/api/v1/groups/{gid}")).status_code == 404
    assert feature.domain.snapshot(OWNER, gid)["group"]["groupId"] == gid
    await http.post("/api/v1/plugins/teams/lifecycle", json={"enabled": True})
    assert (await http.get(f"/api/v1/groups/{gid}")).status_code == 200


@pytest.mark.asyncio
async def test_tool_transport_rejects_json_actor_impersonation(client):
    _, feature = client
    with pytest.raises(TeamsError, match="宿主授予"):
        await feature.invoke(
            {"actor": {"kind": "member", "member_id": "leader"}}, "team_context", {}, "call-one"
        )


async def active_fixture(client):
    http, feature = client
    created = await create(http)
    gid = created["group"]["groupId"]
    started = (
        await http.post(
            f"/api/v1/groups/{gid}/messages",
            json={
                "parts": [{"kind": "text", "text": "review"}],
                "intent": "start_goal",
                "idempotencyKey": "start",
            },
        )
    ).json()
    delivery = feature.domain.snapshot(OWNER, gid)["deliveries"][0]
    feature.domain.project_run(
        delivery_id=delivery["deliveryId"],
        event_id="actual-start",
        run_id="native-run",
        status="running",
    )
    member = feature.domain.snapshot(OWNER, gid)["runMembers"][0]
    actor = Actor(
        OWNER.tenant_id,
        "member:leader",
        kind="member",
        group_id=gid,
        member_id="leader",
        team_run_id=started["teamRunId"],
        run_id="native-run",
    )
    return gid, member, actor, delivery


@pytest.mark.asyncio
async def test_member_cancel_is_scoped_durable_and_does_not_fabricate_terminal(client):
    http, feature = client
    gid, member, _, delivery = await active_fixture(client)
    ref = feature.member_ref(gid, member, "native-run")
    wrong = await http.post(
        f"/api/v1/groups/{gid}/members/reviewer/cancel",
        json={
            "ref": ref,
            "idempotencyKey": "cancel",
        },
    )
    assert wrong.status_code == 403
    payload = {"ref": ref, "idempotencyKey": "cancel"}
    url = f"/api/v1/groups/{gid}/members/leader/cancel"
    receipt = await http.post(url, json=payload)
    assert receipt.status_code == 202 and receipt.json()["status"] == "cancel_requested"
    assert (await http.post(url, json=payload)).json() == receipt.json()
    calls = []

    async def cancel(scope, **arguments):
        calls.append((scope, arguments))
        return ExecutionReceipt("accepted", run_id=arguments["run_id"])

    feature.host.cancel = cancel
    await feature.runtime._member_controls(feature.domain)
    await feature.runtime._member_controls(feature.domain)
    assert len(calls) == 1 and calls[0][1]["run_id"] == "native-run"
    assert feature.domain.snapshot(OWNER, gid)["members"][0]["executionStatus"] == "running"
    feature.domain.project_run(
        delivery_id=delivery["deliveryId"],
        event_id="real-stop",
        run_id="native-run",
        status="cancelled",
    )
    assert feature.domain.snapshot(OWNER, gid)["members"][0]["executionStatus"] == "idle"
    execution = feature.domain.execution(OWNER, gid, delivery["teamRunId"])
    assert (
        next(node for node in execution["nodes"] if node["kind"] == "run")["status"] == "cancelled"
    )


@pytest.mark.asyncio
async def test_artifacts_are_immutable_scoped_copies_and_tool_arguments_are_strict(client):
    http, feature = client
    gid, _, actor, delivery = await active_fixture(client)
    workspace = feature.workspace(actor)
    (workspace / "report.md").write_text("本次验证结果", encoding="utf-8")
    artifact = feature.publish_artifact(actor, "report.md", "报告.md", "publish")
    assert artifact == feature.publish_artifact(actor, "report.md", "报告.md", "publish")
    (workspace / "report.md").write_text("后续修改", encoding="utf-8")
    copy = feature.read_artifact(actor, artifact["artifactId"])
    assert copy["text"] == "本次验证结果"
    assert (workspace / copy["workspacePath"]).read_text() == "本次验证结果"
    assert copy["source"]["runId"] == "native-run"
    downloaded = await http.get(artifact["uri"])
    assert downloaded.content == "本次验证结果".encode()
    snapshot = feature.domain.snapshot(OWNER, gid)
    assert snapshot["artifacts"] == [artifact]
    principal = {"actor": actor, "deliveryId": delivery["deliveryId"]}
    with pytest.raises(TeamsError) as invalid:
        await feature.invoke(principal, "team_message", {"content": "x", "wake": "true"}, "bad")
    assert invalid.value.code == "invalid_tool_arguments"
    assert feature.domain.snapshot(OWNER, gid)["watermark"] == snapshot["watermark"]
    # A fabricated known artifact under another group cannot bypass row scope.
    with feature.domain.store.transaction() as tx:
        record = tx.get("artifact", artifact["artifactId"])
        record["groupId"] = "another-group"
        tx.put("artifact", artifact["artifactId"], record)
    with pytest.raises(TeamsError) as wrong:
        feature.read_artifact(actor, artifact["artifactId"])
    assert wrong.value.code == "artifact_scope_mismatch"


@pytest.mark.asyncio
async def test_execution_context_is_bounded_without_truncating_owner_history(client):
    from ksadk.plugins.teams.context import execution_context

    http, feature = client
    gid, _, actor, _ = await active_fixture(client)
    content = "large evidence " * 5000
    for index in range(20):
        response = await http.post(
            f"/api/v1/groups/{gid}/messages",
            json={
                "parts": [{"kind": "text", "text": content}],
                "intent": "note",
                "teamRunId": actor.team_run_id,
                "idempotencyKey": f"context-note-{index}",
            },
        )
        assert response.status_code == 202
    context = execution_context(feature.domain, actor)
    assert len(context["recentMessages"]) == 4
    assert all(len(message["parts"][0]["text"]) == 500 for message in context["recentMessages"])
    owner = feature.domain.snapshot(OWNER, gid)
    assert len(owner["messages"]) == 21
    assert owner["messages"][-1]["parts"][0]["text"] == content.strip()


@pytest.mark.asyncio
async def test_parallel_context_artifact_and_observer_require_matching_run(client):
    from ksadk.plugins.teams.context import execution_context
    from ksadk.plugins.teams.contracts import MessageInput

    http, feature = client
    gid, member, actor, _ = await active_fixture(client)
    workspace = feature.workspace(actor)
    (workspace / "private.md").write_text("第一项任务独有资料", encoding="utf-8")
    artifact = feature.publish_artifact(actor, "private.md", None, "first-file")
    receipt = feature.domain.send(
        OWNER,
        gid,
        MessageInput(
            parts=[{"kind": "text", "text": "第二项独立任务"}],
            intent="start_goal",
            idempotencyKey="second",
        ),
    )
    other = replace(actor, team_run_id=receipt["teamRunId"], run_id="other-native-run")
    other_snapshot = feature.domain.snapshot(OWNER, gid, other.team_run_id)
    other_member = next(m for m in other_snapshot["runMembers"] if m["memberId"] == "leader")
    assert feature.workspace(other) != workspace
    context = execution_context(feature.domain, other)
    assert context["artifacts"] == []
    assert [m["parts"][0]["text"] for m in context["recentMessages"]] == ["第二项独立任务"]
    with pytest.raises(TeamsError, match="显式引用"):
        feature.read_artifact(other, artifact["artifactId"])
    with pytest.raises(TeamsError):
        feature.member_scope(gid, member["memberId"], other_member["sessionId"], "native-run")
    with pytest.raises(TeamsError, match="群主"):
        feature.domain.send(
            other,
            gid,
            MessageInput(
                parts=[
                    {
                        "kind": "attachment",
                        "attachmentRef": artifact["artifactId"],
                        "mediaType": "text/plain",
                    }
                ],
                intent="note",
                teamRunId=other.team_run_id,
                idempotencyKey="self-share",
            ),
        )
    feature.domain.send(
        OWNER,
        gid,
        MessageInput(
            parts=[
                {
                    "kind": "attachment",
                    "attachmentRef": artifact["artifactId"],
                    "mediaType": "text/plain",
                }
            ],
            intent="note",
            teamRunId=other.team_run_id,
            idempotencyKey="owner-share",
        ),
    )
    assert feature.read_artifact(other, artifact["artifactId"])["text"] == "第一项任务独有资料"
    response = await http.get(f"/api/v1/groups/{gid}", params={"teamRunId": other.team_run_id})
    assert {run["teamRunId"] for run in response.json()["teamRuns"]} == {other.team_run_id}


@pytest.mark.asyncio
async def test_binding_catalog_does_not_instantiate_unselected_agents(client):
    _, feature = client
    calls = []
    original = feature.host.describe

    async def describe(scope):
        calls.append(scope.binding_ref)
        return await original(scope)

    async def catalog():
        return [
            {"bindingRef": "local-build:build-one", "name": "One", "availability": "available"},
            {"bindingRef": "local-build:unselected", "name": "Other"},
        ]

    feature.host.describe = describe
    feature.list_bindings = catalog
    assert len(await feature.bindings()) == 2
    assert calls == []
    result = await feature.validate_bindings(["local-build:build-one", "local-build:build-one"])
    assert calls == ["local-build:build-one"]
    assert result["local-build:build-one"]["name"] == "One"
    with pytest.raises(TeamsError, match="版本已不可用"):
        await feature.validate_bindings(["missing"])
    assert calls == ["local-build:build-one"]


@pytest.mark.asyncio
async def test_application_prepares_frozen_workspace_from_new_task_input(client, tmp_path):
    from ksadk.plugins.teams.contracts import MessageInput

    http, feature = client
    created = await create(http)
    gid = created["group"]["groupId"]
    source = tmp_path / "source"
    source.mkdir()
    (source / "brief.txt").write_text("task input")
    feature.allowed_workspace_roots = [source]
    receipt = feature.domain.send(
        OWNER,
        gid,
        MessageInput(
            parts=[{"kind": "text", "text": "使用所选文件"}],
            intent="start_goal",
            idempotencyKey="with-workspace",
            workspace={"sourcePath": str(source), "inputs": ["brief.txt"]},
        ),
    )
    actor = Actor(
        OWNER.tenant_id,
        "member:leader",
        kind="member",
        group_id=gid,
        team_run_id=receipt["teamRunId"],
        member_id="leader",
        run_id="execution",
    )
    workspace = feature.workspace(actor)
    assert (workspace / "brief.txt").read_text() == "task input"
    assert feature.domain.snapshot(OWNER, gid)["teamRuns"][0]["workspace"]["sourcePath"] == str(
        source
    )
    assert workspace != source


@pytest.mark.asyncio
async def test_server_workspace_requires_commit_and_ambiguous_execution_requires_run(client):
    http, feature = client
    created = await create(http)
    gid = created["group"]["groupId"]
    feature.require_pinned_workspace_commit = True
    payload = {
        "parts": [{"kind": "text", "text": "第一项"}],
        "intent": "start_goal",
        "idempotencyKey": "first",
        "workspace": {"sourcePath": "/controlled/repo"},
    }
    response = await http.post(f"/api/v1/groups/{gid}/messages", json=payload)
    assert (
        response.status_code == 422
        and response.json()["error"]["code"] == "workspace_commit_required"
    )
    assert feature.domain.snapshot(OWNER, gid)["teamRuns"] == []
    payload.pop("workspace")
    assert (await http.post(f"/api/v1/groups/{gid}/messages", json=payload)).status_code == 202
    payload["idempotencyKey"] = "second"
    assert (await http.post(f"/api/v1/groups/{gid}/messages", json=payload)).status_code == 202
    response = await http.get(f"/api/v1/groups/{gid}/execution")
    assert response.status_code == 422 and response.json()["error"]["code"] == "team_run_ambiguous"
