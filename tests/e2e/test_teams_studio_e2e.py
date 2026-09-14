"""Actual Studio Build + Kernel Runs + official DSH tools, with a controlled model.

No Run/receipt/task terminal is fabricated: only user HTTP actions and model
tool calls advance the workflow. The optional public DSH installation is isolated.

Local/browser setup follows the same public composition: install the pinned DSH
toolchain; start Studio; install and enable shipped_harness_dsh_bundle through
Studio.reconfigure_dsh_profile and the web Profile at studio_dsh_home(workspace);
await studio.teams_installation.enable(); build an AgentSpec(runtime.type=harness)
with model/endpoint/credentialRef supplied by the operator; bind the immutable
Build as local-build:<id>. The tests inject only the reasoner; no model request or
real credential is used. Studio's normal UI/server exposes the resulting routes.
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.dsh_home import studio_dsh_home
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
from ksadk.studio.contracts import AgentSpec
from ksadk.studio.kernel_registry import StudioBuildKernelError
from ksadk.studio.service import StudioService

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_DSH_TOOLCHAIN_E2E") != "1",
    reason="set KSADK_DSH_TOOLCHAIN_E2E=1 for the isolated official DSH toolchain",
)


class TeamReasoner:
    def __init__(self, *, members=("one", "two"), approval=False):
        self.members = members
        self.approval = approval
        self.after_write = asyncio.Event()
        self.members_wrote = set()
        self.members_entered = set()
        self.release_members = asyncio.Event()
        self.calls = []
        self.failures = []
        self.child_calls = 0

    async def complete(self, *, prompt, messages, tools, **kwargs):
        if prompt.startswith("Fixture child reviewer"):
            self.child_calls += 1
            assert not tools
            return HarnessReasoningTurn(
                final_text="Independent child evidence",
                usage={"input_tokens": 3, "output_tokens": 2},
            )
        state = json.loads(prompt.rsplit("数据开始：\n", 1)[1])
        assert "team_create_task" in {tool.name for tool in tools}
        results = [message for message in messages if message.get("role") == "tool"]
        for message in results:
            if str(message.get("content", "")).startswith("[error]"):
                self.failures.append(message["content"])
                raise AssertionError(message["content"])
        self.calls.append((state["memberId"], state["teamRunId"], len(results)))

        def turn(*calls):
            return HarnessReasoningTurn(
                tool_calls=tuple(calls), usage={"input_tokens": 10, "output_tokens": 5}
            )

        if state["role"] != "leader":
            if self.approval and not any(
                message.get("name") == "write_workspace_file" for message in results
            ):
                return turn(
                    HarnessToolCall(
                        "approved-write",
                        "write_workspace_file",
                        {
                            "path": "approved-proof.txt",
                            "content": "Approved through the original scoped interaction",
                        },
                    )
                )
            if self.approval:
                self.after_write.set()
                self.members_wrote.add(state["memberId"])
            if not any(message.get("name") == "team_submit_result" for message in results):
                self.members_entered.add(state["memberId"])
                await self.release_members.wait()
                if not any(message.get("name") == "review_helper" for message in results):
                    return turn(
                        HarnessToolCall(
                            "child-review",
                            "review_helper",
                            {"task": "Independently inspect the evidence"},
                        )
                    )
                return turn(
                    HarnessToolCall(
                        "submit-result",
                        "team_submit_result",
                        {
                            "result": f"Verified fixture evidence from {state['memberId']}",
                        },
                    )
                )
            return HarnessReasoningTurn(final_text=f"{state['memberId']} completed")

        if state["tasks"] and all(task["status"] == "succeeded" for task in state["tasks"]):
            if not any(message.get("name") == "team_finish" for message in results):
                return turn(
                    HarnessToolCall(
                        "finish",
                        "team_finish",
                        {
                            "result": "Joined evidence from both accepted member Runs",
                        },
                    )
                )
            return HarnessReasoningTurn(final_text="Final team result awaiting owner acceptance")
        created = []
        for message in results:
            if message.get("name") == "team_create_task":
                created.append(json.loads(message["content"])["taskId"])
        if not created:
            return turn(
                *(
                    HarnessToolCall(
                        f"create-{name}",
                        "team_create_task",
                        {
                            "title": f"Review {name}",
                            "description": "Produce review evidence",
                            "ownerMemberId": name,
                            "acceptanceCriteria": "Human verifies evidence",
                            "acceptancePolicy": state["taskAcceptance"],
                        },
                    )
                    for name in self.members
                )
            )
        if not any(message.get("name") == "team_wait" for message in results):
            return turn(HarnessToolCall("wait-for-reviews", "team_wait", {"taskIds": created}))
        return HarnessReasoningTurn(final_text="Waiting for human acceptance of both reviews")


async def wait_for(predicate, *, timeout=45, detail=lambda: ""):
    try:
        async with asyncio.timeout(timeout):
            while not await predicate():
                await asyncio.sleep(0.05)
    except TimeoutError as error:
        raise AssertionError(f"Timed out waiting for {predicate.__name__}: {detail()}") from error


async def build_fixture(studio, *, approval=False):
    assert any(
        manifest.metadata.id == "io.ksadk.harness-provider"
        for manifest in studio._active_provider_manifests.values()
    )
    draft = studio.create_agent(
        agent_id="team-reviewer",
        name="Teams fixture",
        spec=AgentSpec.model_validate(
            {
                "runtime": {"type": "harness"},
                "subAgents": [
                    {
                        "name": "review_helper",
                        "instructions": "Fixture child reviewer",
                        "tools": [],
                        "maxTurns": 2,
                        "timeoutSeconds": 60,
                        "inheritSkills": False,
                    }
                ],
                "instructions": {"system": "Follow the trusted team execution policy."},
                "model": {
                    "model": "fixture-model",
                    "endpointUrl": "https://model.example.test/v1/chat/completions",
                    "credentialRef": "env://KSADK_TEAMS_E2E_MODEL_KEY",
                },
                "capabilities": {
                    "tools": (
                        [
                            {
                                "name": "write_workspace_file",
                                "version": "1.0.0",
                                "sideEffect": "write",
                                "approval": "always",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "content": {"type": "string"},
                                    },
                                    "required": ["path", "content"],
                                },
                            }
                        ]
                        if approval
                        else []
                    )
                },
                "security": {
                    "allowedPermissions": ["process:host-user"],
                    "network": {"mode": "restricted", "allowedHosts": ["model.example.test"]},
                },
            }
        ),
    )
    operation = studio.submit_build(
        draft.metadata.id, revision=draft.metadata.revision, idempotency_key="fixture-build"
    )

    async def built():
        return studio.operations.get(operation.id).status in {"SUCCEEDED", "FAILED"}

    await wait_for(built)
    result = studio.operations.get(operation.id)
    assert result.status == "SUCCEEDED", result.error
    return result.resource_id


async def enable_harness_provider(studio, toolchain):
    await studio.start()

    def install():
        with DshProfilePluginBridge(
            dsh_home=studio_dsh_home(studio.workspace.root),
            profile="web",
            dsh_command=toolchain.require_command(),
            cwd=studio.workspace.root,
        ) as bridge:
            installed = bridge.install_plugin(
                str(shipped_harness_dsh_bundle().root), accept_host_permissions=True
            )
            bridge.set_enabled(installed.name, enabled=True)

    await studio.reconfigure_dsh_profile(lambda: asyncio.to_thread(install))


@pytest.mark.asyncio
async def test_real_team_parallel_runs_human_acceptance_join_and_restart(tmp_path, monkeypatch):
    toolchains = tmp_path / "toolchains"
    toolchain = DshToolchainManager(base_dir=toolchains)
    await asyncio.to_thread(toolchain.install)
    monkeypatch.setenv("AGENTENGINE_PLUGIN_TOOLCHAIN_HOME", str(toolchains))
    monkeypatch.setenv("KSADK_TEAMS_E2E_MODEL_KEY", "unused-fixture-key")
    for key in ("KSADK_DSH_HOME", "KSADK_DSH_PROFILE", "KSADK_DSH_BIN"):
        monkeypatch.delenv(key, raising=False)
    workspace = tmp_path / "studio"
    reasoner = TeamReasoner()
    studio = StudioService(workspace, harness_reasoner=reasoner)
    try:
        await enable_harness_provider(studio, toolchain)
        await studio.teams_installation.enable()
        assert studio.teams_installation.status()["enabled"]
        feature = studio.teams_installation.application
        assert feature.tool_transport is not None
        build_id = await build_fixture(studio)
        app = FastAPI()
        app.mount("/api/v1", studio.workspace_plugins.api)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            binding = "local-build:" + build_id
            response = await http.post(
                "/api/v1/groups",
                json={
                    "name": "Actual two-member review",
                    "leaderMemberId": "leader",
                    "idempotencyKey": "create-group",
                    "members": [
                        {"memberId": name, "name": name, "bindingRef": binding}
                        for name in ("leader", "one", "two")
                    ],
                },
            )
            assert response.status_code == 201, response.text
            gid = response.json()["group"]["groupId"]
            response = await http.post(
                f"/api/v1/groups/{gid}/messages",
                json={
                    "parts": [{"kind": "text", "text": "Review two independent areas"}],
                    "intent": "start_goal",
                    "idempotencyKey": "goal",
                },
            )
            assert response.status_code == 202, response.text
            team_run_id = response.json()["teamRunId"]

            async def snapshot():
                value = await http.get(f"/api/v1/groups/{gid}")
                assert value.status_code == 200, value.text
                return value.json()

            async def both_running():
                data = await snapshot()
                return (
                    reasoner.members_entered == {"one", "two"}
                    and len(
                        [
                            d
                            for d in data["deliveries"]
                            if d["memberId"] in {"one", "two"} and d.get("runId")
                        ]
                    )
                    == 2
                )

            await wait_for(both_running, detail=lambda: reasoner.failures)
            running = await snapshot()
            members = {m["memberId"]: m for m in running["members"]}
            assert len({m["sessionId"] for m in members.values()}) == 3
            member_run_ids = []
            runtime = studio.scheduler_runtimes.runtime_for_build(build_id)
            for delivery in running["deliveries"]:
                if delivery["memberId"] not in {"one", "two"}:
                    continue
                run_id = delivery["runId"]
                UUID(run_id)
                record = await runtime.kernel_store.load_run(run_id)
                assert record is not None and record.state.value == "running"
                assert record.session_id == members[delivery["memberId"]]["sessionId"]
                member_run_ids.append(run_id)
            assert len(set(member_run_ids)) == 2
            reasoner.release_members.set()

            async def awaiting_humans():
                data = await snapshot()
                return len(data["tasks"]) == 2 and all(
                    t["status"] == "awaiting_acceptance" for t in data["tasks"]
                )

            await wait_for(awaiting_humans, detail=lambda: reasoner.failures)
            pending = await snapshot()
            assert reasoner.child_calls == 2
            child_nodes = []

            async def child_projection_completed():
                nonlocal child_nodes
                tree_response = await http.get(
                    f"/api/v1/groups/{gid}/execution", params={"teamRunId": team_run_id}
                )
                assert tree_response.status_code == 200, tree_response.text
                child_nodes = [
                    node
                    for node in tree_response.json()["nodes"]
                    if node["kind"] == "child_invocation"
                ]
                return len(child_nodes) == 2 and all(
                    node.get("parentNodeId") and node["status"] == "succeeded"
                    for node in child_nodes
                )

            # Task reconciliation commits before cursor-driven child projection.
            # Verify its own convergence instead of assuming one atomic snapshot.
            await wait_for(child_projection_completed, detail=lambda: child_nodes)
            assert len(child_nodes) == 2
            assert {node["source"]["runId"] for node in child_nodes} == set(member_run_ids)
            assert all(
                node.get("parentNodeId") and node["status"] == "succeeded" for node in child_nodes
            )
            for task in pending["tasks"]:
                result = await http.post(
                    f"/api/v1/groups/{gid}/tasks/{task['taskId']}/actions",
                    json={
                        "action": "accept",
                        "expectedRevision": task["revision"],
                        "idempotencyKey": "accept-" + task["taskId"],
                    },
                )
                assert result.status_code == 200, result.text

            async def final_candidate():
                data = await snapshot()
                return data["teamRuns"][0]["status"] == "awaiting_acceptance"

            await wait_for(final_candidate, detail=lambda: reasoner.failures)
            data = await snapshot()
            result = await http.post(
                f"/api/v1/groups/{gid}/team-runs/{team_run_id}/acceptance",
                json={
                    "accepted": True,
                    "expectedRevision": data["teamRuns"][0]["revision"],
                    "idempotencyKey": "owner-accept",
                },
            )
            assert result.status_code == 200, result.text
            settled = await snapshot()
            assert settled["teamRuns"][0]["status"] == "succeeded"
            assert len([d for d in settled["deliveries"] if d["memberId"] == "leader"]) == 2
            watermark = settled["watermark"]
    finally:
        await studio.aclose()

    # A new actual Studio process composition reads the same durable facts.
    restored = StudioService(workspace, harness_reasoner=reasoner)
    try:
        await restored.teams_installation.enable()
        feature = restored.teams_installation.application
        state = feature.domain.snapshot(feature.actor(), gid)
        assert state["teamRuns"][0]["status"] == "succeeded"
        assert state["watermark"] >= watermark
        assert {d["runId"] for d in state["deliveries"] if d["memberId"] in {"one", "two"}} == set(
            member_run_ids
        )
        with pytest.raises(Exception, match="执行不属于此成员"):
            feature.member_scope(gid, "leader", members["leader"]["sessionId"], member_run_ids[0])
        await verify_live_stop(restored, build_id, reasoner)
    finally:
        await restored.aclose()


async def verify_live_stop(studio, build_id, reasoner):
    """Stop both actual running members; suspension alone keeps them running."""
    reasoner.members_entered.clear()
    reasoner.release_members.clear()
    feature = studio.teams_installation.application
    app = FastAPI()
    app.mount("/api/v1", studio.workspace_plugins.api)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/api/v1/groups",
            json={
                "name": "Stop actual parallel runs",
                "leaderMemberId": "leader",
                "idempotencyKey": "stop-group",
                "members": [
                    {"memberId": name, "name": name, "bindingRef": "local-build:" + build_id}
                    for name in ("leader", "one", "two")
                ],
            },
        )
        assert response.status_code == 201, response.text
        gid = response.json()["group"]["groupId"]
        response = await http.post(
            f"/api/v1/groups/{gid}/messages",
            json={
                "parts": [{"kind": "text", "text": "Hold both members until the owner stops"}],
                "intent": "start_goal",
                "idempotencyKey": "goal",
            },
        )
        assert response.status_code == 202, response.text
        team_run_id = response.json()["teamRunId"]

        async def started():
            state = feature.domain.snapshot(feature.actor(), gid)
            return (
                reasoner.members_entered == {"one", "two"}
                and len([d for d in state["deliveries"] if d.get("runId")]) == 3
            )

        await wait_for(started, detail=lambda: (reasoner.failures, feature.runtime.last_error))
        state = feature.domain.snapshot(feature.actor(), gid)
        run_ids = [d["runId"] for d in state["deliveries"] if d["memberId"] in {"one", "two"}]
        runtime = studio.scheduler_runtimes.runtime_for_build(build_id)
        response = await http.post(
            f"/api/v1/groups/{gid}/team-runs/{team_run_id}/control",
            json={
                "action": "suspend_dispatch",
                "expectedRevision": state["teamRuns"][0]["revision"],
                "idempotencyKey": "pause-dispatch",
            },
        )
        assert response.status_code == 202, response.text

        async def suspended():
            with feature.domain.store.transaction() as tx:
                controls = tx.list("control", gid, team_run_id=team_run_id)
            return any(
                c["action"] == "suspend_dispatch" and c["status"] == "completed" for c in controls
            )

        await wait_for(suspended, detail=lambda: feature.runtime.last_error)
        records = [await runtime.kernel_store.load_run(run_id) for run_id in run_ids]
        assert all(record.state.value == "running" for record in records)
        state = feature.domain.snapshot(feature.actor(), gid)
        response = await http.post(
            f"/api/v1/groups/{gid}/team-runs/{team_run_id}/control",
            json={
                "action": "stop",
                "expectedRevision": state["teamRuns"][0]["revision"],
                "idempotencyKey": "stop",
            },
        )
        assert response.status_code == 202, response.text

        async def stopped():
            state = feature.domain.snapshot(feature.actor(), gid)
            records = [await runtime.kernel_store.load_run(run_id) for run_id in run_ids]
            return state["teamRuns"][0]["status"] == "cancelled" and all(
                r.state.value == "cancelled" for r in records
            )

        await wait_for(stopped, detail=lambda: (reasoner.failures, feature.runtime.last_error))
        state = feature.domain.snapshot(feature.actor(), gid)
        assert all(t["status"] == "cancelled" for t in state["tasks"])
        assert len(state["deliveries"]) == 3
        assert not reasoner.release_members.is_set()
        # Irreversible grant revocation must reject a later resume request.
        response = await http.post(
            f"/api/v1/groups/{gid}/team-runs/{team_run_id}/control",
            json={
                "action": "resume_dispatch",
                "expectedRevision": state["teamRuns"][0]["revision"],
                "idempotencyKey": "resume-after-stop",
            },
        )
        assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_real_scoped_approval_restores_then_stop_cancels_native_run(tmp_path, monkeypatch):
    toolchains = tmp_path / "toolchains"
    toolchain = DshToolchainManager(base_dir=toolchains)
    await asyncio.to_thread(toolchain.install)
    monkeypatch.setenv("AGENTENGINE_PLUGIN_TOOLCHAIN_HOME", str(toolchains))
    monkeypatch.setenv("KSADK_TEAMS_E2E_MODEL_KEY", "unused-fixture-key")
    for key in ("KSADK_DSH_HOME", "KSADK_DSH_PROFILE", "KSADK_DSH_BIN"):
        monkeypatch.delenv(key, raising=False)
    workspace = tmp_path / "studio"
    reasoner = TeamReasoner(members=("one", "two"), approval=True)
    studio = StudioService(workspace, harness_reasoner=reasoner)
    try:
        await enable_harness_provider(studio, toolchain)
        await studio.teams_installation.enable()
        build_id = await build_fixture(studio, approval=True)
        app = FastAPI()
        app.mount("/api/v1", studio.workspace_plugins.api)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/api/v1/groups",
                json={
                    "name": "Approval and stop",
                    "leaderMemberId": "leader",
                    "idempotencyKey": "create-group",
                    "members": [
                        {"memberId": name, "name": name, "bindingRef": "local-build:" + build_id}
                        for name in ("leader", "one", "two")
                    ],
                },
            )
            assert response.status_code == 201, response.text
            gid = response.json()["group"]["groupId"]
            response = await http.post(
                f"/api/v1/groups/{gid}/messages",
                json={
                    "parts": [{"kind": "text", "text": "Write evidence after scoped approval"}],
                    "intent": "start_goal",
                    "idempotencyKey": "goal",
                },
            )
            assert response.status_code == 202, response.text
            team_run_id = response.json()["teamRunId"]
            feature = studio.teams_installation.application

            async def awaiting_approval():
                state = feature.domain.snapshot(feature.actor(), gid)
                return len([i for i in state["interactions"] if i["status"] == "pending"]) == 2

            await wait_for(
                awaiting_approval, detail=lambda: (reasoner.failures, feature.runtime.last_error)
            )
            state = feature.domain.snapshot(feature.actor(), gid)
            references = {
                i["ref"]["memberId"]: i["ref"]
                for i in state["interactions"]
                if i["status"] == "pending"
            }
            assert set(references) == {"one", "two"}
            member_run_ids = {member: ref["runId"] for member, ref in references.items()}
            assert len(set(member_run_ids.values())) == 2
            assert not list(workspace.rglob("approved-proof.txt"))
            for member_run_id in member_run_ids.values():
                record = await studio.scheduler_runtimes.runtime_for_build(
                    build_id
                ).kernel_store.load_run(member_run_id)
                assert record.state.value in {"paused", "waiting"}
            assert len(state["deliveries"]) == 3
    finally:
        await studio.aclose()

    # Recreate the whole composition; the pending interaction must resume the
    # existing native checkpoints for BOTH sessions of the same Build. Selecting
    # the first open Build Run twice must not masquerade as successful recovery.
    restored = StudioService(workspace, harness_reasoner=reasoner)
    try:
        await restored.teams_installation.enable()
        feature = restored.teams_installation.application
        app = FastAPI()
        app.mount("/api/v1", restored.workspace_plugins.api)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:

            async def recovered():
                state = feature.domain.snapshot(feature.actor(), gid)
                try:
                    executions = restored.scheduler_runtimes.runtime_for_build(
                        build_id
                    ).worker._executions
                except StudioBuildKernelError as error:
                    if error.code not in {"TARGET_UNAVAILABLE", "SCHEDULER_TARGET_UNAVAILABLE"}:
                        raise
                    return False  # Background recovery has not registered this Build yet.
                return all(
                    any(i["ref"] == ref and i["status"] == "pending" for i in state["interactions"])
                    and ref["runId"] in executions
                    and executions[ref["runId"]].handle.session_id == ref["sessionId"]
                    for ref in references.values()
                )

            await wait_for(recovered, detail=lambda: feature.runtime.last_error)
            state = feature.domain.snapshot(feature.actor(), gid)
            ref = references["one"]
            interaction = next(i for i in state["interactions"] if i["ref"] == ref)
            tampered = {**ref, "memberId": "leader"}
            response = await http.post(
                f"/api/v1/groups/{gid}/interactions",
                json={
                    "ref": tampered,
                    "action": "approve",
                    "expectedRevision": interaction["revision"],
                    "idempotencyKey": "wrong-scope",
                },
            )
            assert response.status_code == 403, response.text
            for member, ref in references.items():
                interaction = next(i for i in state["interactions"] if i["ref"] == ref)
                response = await http.post(
                    f"/api/v1/groups/{gid}/interactions",
                    json={
                        "ref": ref,
                        "action": "approve",
                        "expectedRevision": interaction["revision"],
                        "idempotencyKey": "approve-original-" + member,
                    },
                )
                assert response.status_code == 202, response.text
                assert response.json()["status"] in {"accepted", "duplicate"}, response.text

            async def wrote():
                return reasoner.members_wrote == {"one", "two"}

            await wait_for(
                wrote,
                detail=lambda: (
                    reasoner.failures,
                    feature.runtime.last_error,
                    feature.domain.snapshot(feature.actor(), gid),
                ),
            )
            files = list(workspace.rglob("approved-proof.txt"))
            assert len(files) == 2
            for member, member_run_id in member_run_ids.items():
                output = (
                    workspace / ".agentkit/plugins/teams/workspaces" / gid
                    / member / member_run_id / "approved-proof.txt"
                )
                assert output.read_text() == "Approved through the original scoped interaction"
            state = feature.domain.snapshot(feature.actor(), gid)
            assert len(state["deliveries"]) == 3
            assert all(i["status"] == "resolved" for i in state["interactions"])
            kernel_runtime = restored.scheduler_runtimes.runtime_for_build(build_id)
            for member, member_run_id in member_run_ids.items():
                assert [d["runId"] for d in state["deliveries"] if d["memberId"] == member] == [
                    member_run_id
                ]
                record = await kernel_runtime.kernel_store.load_run(member_run_id)
                assert record.state.value == "running"
            run = next(r for r in state["teamRuns"] if r["teamRunId"] == team_run_id)
            response = await http.post(
                f"/api/v1/groups/{gid}/team-runs/{team_run_id}/control",
                json={
                    "action": "stop",
                    "expectedRevision": run["revision"],
                    "idempotencyKey": "stop",
                },
            )
            assert response.status_code == 202, response.text

            async def cancelled():
                state = feature.domain.snapshot(feature.actor(), gid)
                records = [
                    await kernel_runtime.kernel_store.load_run(member_run_id)
                    for member_run_id in member_run_ids.values()
                ]
                return (
                    state["teamRuns"][0]["status"] == "cancelled"
                    and all(record.state.value == "cancelled" for record in records)
                )

            await wait_for(
                cancelled, detail=lambda: (reasoner.failures, feature.runtime.last_error)
            )
            state = feature.domain.snapshot(feature.actor(), gid)
            assert len(state["tasks"]) == 2
            assert all(task["status"] == "cancelled" for task in state["tasks"])
            assert not reasoner.release_members.is_set()
            stopped_count = len(state["deliveries"])
    finally:
        await restored.aclose()

    final = StudioService(workspace, harness_reasoner=reasoner)
    try:
        await final.teams_installation.enable()
        feature = final.teams_installation.application
        state = feature.domain.snapshot(feature.actor(), gid)
        assert state["teamRuns"][0]["status"] == "cancelled"
        assert len(state["deliveries"]) == stopped_count
        for member, member_run_id in member_run_ids.items():
            assert [d["runId"] for d in state["deliveries"] if d["memberId"] == member] == [
                member_run_id
            ]
        assert len(list(workspace.rglob("approved-proof.txt"))) == 2
    finally:
        await final.aclose()
