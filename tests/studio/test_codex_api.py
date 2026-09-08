from __future__ import annotations

import asyncio
import io
import time
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    OutputRef,
    RunCompleted,
    RunStarted,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.runtime import RunHandle, StartRequest
from ksadk.studio.api import RunRequest, create_studio_app
from ksadk.studio.cloud import InMemoryCloudGateway
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.contracts import CapabilityBinding, MCPServerRef, ModelSpec, RunStatus
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import (
    RuntimeFixture,
    standard_codex_events,
)


def _manifest(prompt: str = "检查 src/demo.py，只报告确定的问题。\n") -> dict:
    return {
        "name": "review-helper",
        "version": "1.0.0",
        "framework": "codex",
        "artifact_type": "ManagedRuntime",
        "runtime": {"name": "codex", "version": "0.144.4"},
        "model": "glm-5.2",
        "prompt": prompt,
    }


def _inspector(_runtime) -> tuple[str, str, str]:
    return "0.8.0", "0.144.4", "codex-cli 0.144.4"


def _skill_archive() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "review/SKILL.md",
            "---\n"
            "name: Review Skill\n"
            "description: Review carefully\n"
            "version: 1.0.0\n"
            "---\n"
            "Review.\n",
        )
    return stream.getvalue()


async def _slow_codex_events(
    request: StartRequest,
    handle: RunHandle,
):
    common = {
        "schema_version": 2,
        "timestamp": 1.0,
        "run_id": handle.run_id,
        "scope_id": f"scope-{handle.run_id}",
    }
    source = SourceRef(framework="codex")
    yield RunStarted(
        event_id="e1",
        seq=1,
        status="running",
        source=source,
        **common,
    )
    await asyncio.sleep(0.1)
    yield ItemCompleted(
        event_id="e2",
        seq=2,
        item_id="msg-1",
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text="刷新不会中断。"),)),
        source=source,
        **common,
    )
    yield RunCompleted(
        event_id="e3",
        seq=3,
        status="completed",
        output_refs=(
            OutputRef(
                scope_id=common["scope_id"],
                item_id="msg-1",
                part_id="text-0",
            ),
        ),
        source=source,
        **common,
    )


def _wait(client: TestClient, operation_id: str) -> dict:
    for _ in range(300):
        operation = client.get(f"/api/v1/operations/{operation_id}").json()
        if operation["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return operation
        time.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not finish")


def test_codex_api_create_validate_build_run_and_trace_flow(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/demo.py").write_text(
        "def average(values):\n    return sum(values) / len(values)\n",
        encoding="utf-8",
    )
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        saved = client.put("/api/v1/codex/manifest", json=_manifest())
        assert saved.status_code == 200
        source_sha = saved.json()["manifestSha256"]
        assert saved.json()["buildCurrent"] is False
        assert (tmp_path / "agentengine.yaml").is_file()
        assert not list((tmp_path / "agents").rglob("agent.yaml"))

        validation = client.post(
            "/api/v1/codex/manifest:validate",
            json=_manifest(),
        )
        assert validation.json() == {"valid": True, "diagnostics": []}

        build_operation = client.post(
            "/api/v1/codex/builds",
            headers={"Idempotency-Key": "codex-build-1"},
        )
        assert build_operation.status_code == 202
        build_done = _wait(client, build_operation.json()["id"])
        assert build_done["status"] == "SUCCEEDED"
        build = client.get(f"/api/v1/codex/builds/{build_done['resourceId']}").json()
        assert build["manifestSha256"] == source_sha
        assert build["runtimeLock"]["manifest_sha256"] == source_sha

        current = client.get("/api/v1/codex/manifest").json()
        assert current["buildCurrent"] is True
        assert current["latestBuild"]["id"] == build["id"]

        run_operation = client.post(
            f"/api/v1/codex/builds/{build['id']}/runs",
            headers={"Idempotency-Key": "codex-run-1"},
            json={
                "sessionId": "ses-api",
                "input": {"role": "user", "content": "请审查 src/demo.py"},
                "environment": "local",
                "stream": True,
            },
        )
        assert run_operation.status_code == 202
        run_done = _wait(client, run_operation.json()["id"])
        assert run_done["status"] == "SUCCEEDED"
        run = client.get(f"/api/v1/runs/{run_done['resourceId']}").json()
        assert run["output"] == "发现除零风险。请先检查空列表。"
        assert run["manifestSha256"] == source_sha
        trace = client.get(f"/api/v1/traces/{run['traceId']}").json()
        assert any(span["name"] == "execute_tool codex.command" for span in trace["spans"])
        root = next(span for span in trace["spans"] if span["parentSpanId"] is None)
        assert root["attributes"]["agentkit.manifest.sha256"] == source_sha


def test_codex_managed_runtime_rollback_reuses_target_build_declaration(
    tmp_path: Path,
) -> None:
    """Rollback is an UpdateAgent declaration replacement, never a ZIP upload."""

    cloud = InMemoryCloudGateway()
    service = StudioService(
        tmp_path,
        cloud_gateway=cloud,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    target = {"target": {"region": "pre-online", "environment": "preproduction"}}

    with TestClient(app) as client:
        first = client.put("/api/v1/codex/manifest", json=_manifest("first prompt\n"))
        assert first.status_code == 200
        build_one_op = client.post(
            "/api/v1/codex/builds", headers={"Idempotency-Key": "rollback-build-one"}
        ).json()
        build_one = _wait(client, build_one_op["id"])["resourceId"]

        second = client.put("/api/v1/codex/manifest", json=_manifest("second prompt\n"))
        assert second.status_code == 200
        build_two_op = client.post(
            "/api/v1/codex/builds", headers={"Idempotency-Key": "rollback-build-two"}
        ).json()
        build_two = _wait(client, build_two_op["id"])["resourceId"]

        deployed_op = client.post(
            f"/api/v1/builds/{build_two}/deployments",
            json=target,
            headers={"Idempotency-Key": "rollback-deploy-two"},
        ).json()
        deployment_id = _wait(client, deployed_op["id"])["resourceId"]

        rollback_op = client.post(
            f"/api/v1/deployments/{deployment_id}:rollback",
            json={"targetBuildId": build_one},
            headers={"Idempotency-Key": "rollback-to-one"},
        ).json()
        assert "id" in rollback_op, rollback_op
        completed = _wait(client, rollback_op["id"])
        assert completed["status"] == "SUCCEEDED"
        rolled_back = client.get(f"/api/v1/deployments/{completed['resourceId']}").json()
        assert rolled_back["buildId"] == build_one
        assert rolled_back["artifactId"] == "managed-runtime"
        assert len(cloud.deployments) == 2


def test_original_studio_routes_drive_the_codex_manifest_build_and_runtime(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/demo.py").write_text(
        "def average(values):\n    return sum(values) / len(values)\n",
        encoding="utf-8",
    )
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    agent_payload = {
        "id": "review-helper",
        "name": "Review Helper",
        "description": "只读代码审查",
        "template": "blank",
        "spec": {
            "runtime": {"type": "codex", "version": "0.144.4"},
            "description": "只读代码审查",
            "instructions": {
                "system": "检查 src/demo.py，只报告确定的问题。",
                "task": "先读取文件，再给出最小修复建议。",
            },
            "bindings": {},
        },
    }

    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/system/bootstrap").json()
        assert bootstrap["features"]["runtimeRegistry"] is True
        assert bootstrap["features"]["reactChat"] is True
        assert "sharedChat" not in bootstrap["features"]
        assert {item["runtimeType"] for item in bootstrap["runtimes"]} == {"codex"}

        created = client.post("/api/v1/agents", json=agent_payload)
        assert created.status_code == 201
        assert created.json()["metadata"]["id"] == "review-helper"
        assert (tmp_path / "agentengine.yaml").is_file()
        assert not list((tmp_path / "agents").rglob("agent.yaml"))

        listed = client.get("/api/v1/agents").json()["items"]
        assert [item["metadata"]["id"] for item in listed] == ["review-helper"]
        detail = client.get("/api/v1/agents/review-helper").json()
        assert detail["draft"]["spec"]["instructions"]["system"].startswith("检查 src/demo.py")
        assert detail["builds"] == []

        build_operation = client.post(
            "/api/v1/agents/review-helper/builds",
            headers={"Idempotency-Key": "original-ui-build"},
            json={"revision": 1, "runEvaluation": False},
        ).json()
        build_done = _wait(client, build_operation["id"])
        assert build_done["status"] == "SUCCEEDED"
        build = client.get(f"/api/v1/builds/{build_done['resourceId']}").json()
        assert build["agentId"] == "review-helper"
        assert build["status"] == "SUCCEEDED"
        assert build["bundleDigest"].startswith("sha256:")

        run_operation = client.post(
            f"/api/v1/builds/{build['id']}/runs",
            headers={"Idempotency-Key": "original-ui-run"},
            json={
                "sessionId": "ses-original-ui",
                "input": {"role": "user", "content": "请审查 src/demo.py"},
                "environment": "local",
                "stream": True,
            },
        ).json()
        run_done = _wait(client, run_operation["id"])
        run = client.get(f"/api/v1/runs/{run_done['resourceId']}").json()
        assert run["status"] == "COMPLETED"
        assert run["output"].startswith("发现除零风险")
        trace = client.get(f"/api/v1/traces/{run['traceId']}").json()
        assert any(span["name"] == "execute_tool codex.command" for span in trace["spans"])


def test_codex_studio_creates_lists_and_builds_multiple_yaml_agents(
    tmp_path: Path,
) -> None:
    """Break caught: Create Agent is hard-coded to one root manifest forever."""

    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    def payload(agent_id: str, prompt: str) -> dict:
        return {
            "id": agent_id,
            "name": agent_id,
            "description": prompt,
            "template": "blank",
            "spec": {
                "runtime": {"type": "codex", "version": "0.144.4"},
                "description": prompt,
                "instructions": {"system": prompt, "task": ""},
                "bindings": {},
            },
        }

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/agents",
            json=payload("review-helper", "执行代码审查。"),
        )
        second = client.post(
            "/api/v1/agents",
            json=payload("research-helper", "执行资料研究。"),
        )

        assert first.status_code == 201
        assert second.status_code == 201
        assert (tmp_path / "agentengine.yaml").is_file()
        assert (tmp_path / "agents/research-helper/agentengine.yaml").is_file()
        listed = client.get("/api/v1/agents").json()["items"]
        assert [item["metadata"]["id"] for item in listed] == [
            "review-helper",
            "research-helper",
        ]
        assert (
            client.get("/api/v1/agents/review-helper").json()["draft"]["spec"]["instructions"][
                "system"
            ]
            == "执行代码审查。"
        )
        assert (
            client.get("/api/v1/agents/research-helper").json()["draft"]["spec"]["instructions"][
                "system"
            ]
            == "执行资料研究。"
        )

        operation = client.post(
            "/api/v1/agents/research-helper/builds",
            headers={"Idempotency-Key": "research-helper-build"},
            json={"revision": 1, "runEvaluation": False},
        ).json()
        completed = _wait(client, operation["id"])
        assert completed["status"] == "SUCCEEDED"
        build = client.get(f"/api/v1/builds/{completed['resourceId']}").json()
        assert build["agentId"] == "research-helper"
        assert client.get("/api/v1/agents/review-helper").json()["builds"] == []


def test_codex_api_edit_marks_build_stale_and_rejects_secret_fields(tmp_path: Path) -> None:
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        client.put("/api/v1/codex/manifest", json=_manifest())
        operation = client.post(
            "/api/v1/codex/builds",
            headers={"Idempotency-Key": "codex-build-original"},
        ).json()
        build_id = _wait(client, operation["id"])["resourceId"]

        edited = client.put(
            "/api/v1/codex/manifest",
            json=_manifest("新的 prompt。\n"),
        )
        assert edited.json()["buildCurrent"] is False

        stale_run = client.post(
            f"/api/v1/codex/builds/{build_id}/runs",
            headers={"Idempotency-Key": "codex-stale-run"},
            json={"input": {"content": "review"}},
        )
        assert _wait(client, stale_run.json()["id"])["status"] == "FAILED"

        unsafe = _manifest()
        unsafe["api_key"] = "never-accepted"
        response = client.put("/api/v1/codex/manifest", json=unsafe)
        assert response.status_code == 422
        assert "never-accepted" not in response.text


def test_codex_stream_endpoint_emits_live_trace_events(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/demo.py").write_text("value = 1\n", encoding="utf-8")
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        saved = client.put("/api/v1/codex/manifest", json=_manifest()).json()
        build_operation = client.post(
            "/api/v1/codex/builds",
            headers={"Idempotency-Key": "stream-build"},
        ).json()
        build_id = _wait(client, build_operation["id"])["resourceId"]
        with client.stream(
            "POST",
            f"/api/v1/builds/{build_id}/run:stream",
            headers={"Idempotency-Key": "stream-run"},
            json={
                "sessionId": "ses-stream",
                "input": {"content": "请审查 src/demo.py"},
            },
        ) as response:
            stream = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: run.created" in stream
        assert "event: thinking.delta" in stream
        assert "event: command.started" in stream
        assert "event: message.delta" in stream
        assert "event: run.completed" in stream
        assert saved["manifestSha256"] in stream
        run = client.get("/api/v1/runs?sessionId=ses-stream").json()["items"][0]
        assert f'"traceId":"{run["traceId"]}"' in stream

        legacy = client.post(
            f"/api/v1/codex/builds/{build_id}/run:stream",
            headers={"Idempotency-Key": "legacy-stream-route"},
            json={"input": {"content": "legacy route must not exist"}},
        )
        assert legacy.status_code == 404


@pytest.mark.asyncio
async def test_closing_stream_does_not_cancel_background_run(tmp_path: Path) -> None:
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(_slow_codex_events).executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    build = service.codex_builder.build()
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", "") == "/api/v1/builds/{build_id}/run:stream"
    )

    response = await endpoint(
        build.id,
        RunRequest(
            session_id="ses-refresh",
            input={"content": "刷新页面"},
        ),
        "refresh-stream",
    )
    iterator = response.body_iterator
    first_event = await anext(iterator)
    assert "event: run.created" in first_event
    await iterator.aclose()

    for _ in range(100):
        runs = service.event_store.list_runs(session_id="ses-refresh")
        if runs and runs[0].status != RunStatus.RUNNING:
            break
        await asyncio.sleep(0.01)
    assert runs[0].status == RunStatus.COMPLETED
    assert runs[0].output == "刷新不会中断。"


@pytest.mark.asyncio
async def test_reloading_after_first_responses_event_keeps_run_recoverable(
    tmp_path: Path,
) -> None:
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(_slow_codex_events).executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    service.codex_builder.build()
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", "") == "/v1/responses"
    )

    response = await endpoint(
        {
            "model": "glm-5.2",
            "input": "刷新页面",
            "stream": True,
            "metadata": {
                "agent_id": "review-helper",
                "session_id": "ses-responses-refresh",
                "invocation_id": "resp-refresh",
            },
        }
    )
    iterator = response.body_iterator
    first_event = await anext(iterator)
    assert "event: response.created" in first_event
    await iterator.aclose()

    runs = []
    for _ in range(120):
        runs = service.event_store.list_runs(session_id="ses-responses-refresh")
        if runs and runs[0].status != RunStatus.RUNNING:
            break
        await asyncio.sleep(0.01)
    assert runs[0].status == RunStatus.COMPLETED
    assert runs[0].output == "刷新不会中断。"


def test_openai_responses_endpoint_is_the_public_runtime_contract(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/demo.py").write_text("value = 1\n", encoding="utf-8")
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents",
            json={
                "id": "review-helper",
                "name": "Review Helper",
                "description": "只读代码审查",
                "template": "blank",
                "spec": {
                    "runtime": {"type": "codex", "version": "0.144.4"},
                    "description": "只读代码审查",
                    "instructions": {
                        "system": "检查 src/demo.py，只报告确定的问题。",
                        "task": "先读取文件，再给出最小修复建议。",
                    },
                    "bindings": {},
                },
            },
        )
        assert created.status_code == 201
        bound_model = created.json()["metadata"]["labels"]["agentkit.ksyun.com/model"]
        surface = client.get(
            "/api/v1/agents/review-helper/conversation-surface",
            params={"sessionId": "ses-openai-responses"},
        )
        assert surface.status_code == 200, surface.text
        assert {item["name"] for item in surface.json()["surface"]["inputs"]} == {
            "text",
            "attachment.image",
            "attachment.file",
            "model.select",
            "reasoning.effort",
            "approval",
            "goal",
            "plan",
        }

        with client.stream(
            "POST",
            "/v1/responses",
            json={
                "model": bound_model,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "请审查 src/demo.py"}],
                    }
                ],
                "metadata": {
                    "agent_id": "review-helper",
                    "session_id": "ses-openai-responses",
                    "invocation_id": "resp-openai-stream",
                },
                "stream": True,
            },
        ) as response:
            stream = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: response.created" in stream
        assert "event: response.output_text.delta" in stream
        assert "event: response.output_item.added" in stream
        assert "event: response.output_item.done" in stream
        assert '"type":"shell_call"' in stream
        assert "sed -n '1,80p' src/demo.py" in stream
        assert stream.count("event: response.output_text.delta") >= 2
        assert "发现除零风险" in stream
        assert "event: response.completed" in stream
        assert '"object":"response"' in stream
        assert stream.count('"id":"resp-openai-stream"') >= 2
        assert stream.count('"item_id":"msg_resp-openai-stream"') >= 2
        assert '"id":"msg_resp-openai-stream"' in stream
        assert '"session_id":"ses-openai-responses"' in stream

        non_stream = client.post(
            "/v1/responses",
            json={
                "input": "再审查一次",
                "metadata": {"agent_id": "review-helper"},
                "previous_response_id": "resp-openai-stream",
            },
        )
        assert non_stream.status_code == 200, non_stream.text
        payload = non_stream.json()
        assert payload["object"] == "response"
        assert payload["status"] == "completed"
        assert payload["output"][0]["content"][0]["type"] == "output_text"
        assert payload["output"][0]["content"][0]["text"].startswith("发现除零风险")
        assert payload["metadata"]["session_id"] == "ses-openai-responses"


def test_openai_responses_model_selects_real_bound_codex_model(tmp_path: Path) -> None:
    """Break caught: the public model field only relabels the response envelope."""

    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    manifest = _manifest()
    manifest["models"] = ["glm-5.2", "kimi-k2-code"]
    service.save_codex_manifest(CodexAgentManifest.model_validate(manifest))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        models = client.get("/api/v1/agents/review-helper/models")
        assert models.status_code == 200
        assert [item["id"] for item in models.json()["Models"]] == [
            "glm-5.2",
            "kimi-k2-code",
        ]
        assert models.json()["Current"] == "glm-5.2"

        response = client.post(
            "/v1/responses",
            json={
                "model": "kimi-k2-code",
                "input": "使用绑定的第二个模型",
                "metadata": {"agent_id": "review-helper"},
            },
        )

        assert response.status_code == 200
        assert response.json()["model"] == "kimi-k2-code"
        assert runtime_fixture.start_requests[0].model == "kimi-k2-code"
        run = service.event_store.list_runs()[0]
        assert run.model == "kimi-k2-code"


@pytest.mark.parametrize(
    ("approval_mode", "sandbox", "codex_approval"),
    [
        ("ask", "workspace-write", "manual"),
        ("risk", "workspace-write", "auto_review"),
        ("full", "full-access", "deny_all"),
    ],
)
def test_openai_responses_applies_turn_scoped_approval_mode(
    tmp_path: Path,
    approval_mode: str,
    sandbox: str,
    codex_approval: str,
) -> None:
    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "glm-5.2",
                "input": f"使用 {approval_mode} 批准模式",
                "metadata": {
                    "agent_id": "review-helper",
                    "approval_mode": approval_mode,
                },
            },
        )

    assert response.status_code == 200
    request = runtime_fixture.start_requests[0]
    assert request.config["sandbox"] == sandbox
    assert request.config["sandbox_read_only"] is False
    assert request.config["approval_mode"] == codex_approval
    assert request.config["tool_approval_mode"] == approval_mode
    conversation = request.conversation_preprocessing()
    assert conversation is not None
    assert conversation.request_metadata["tool_approval_mode"] == approval_mode


def test_openai_responses_reads_shared_web_agentengine_metadata(tmp_path: Path) -> None:
    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "glm-5.2",
                "input": "使用共享 Web 的风险确认和计划模式",
                "metadata": {
                    "agent_id": "review-helper",
                    "agentengine": {
                        "tool_approval_mode": "risk",
                        "collaboration_mode": "plan",
                        "goal_objective": "完成协议回归",
                    },
                },
            },
        )

    assert response.status_code == 200
    request = runtime_fixture.start_requests[0]
    assert request.config["sandbox"] == "workspace-write"
    assert request.config["sandbox_read_only"] is False
    assert request.config["approval_mode"] == "auto_review"
    assert request.config["tool_approval_mode"] == "risk"
    assert request.config["collaboration_mode"] == "plan"
    assert request.config["goal_objective"] == "完成协议回归"


def test_run_agent_reads_shared_web_turn_controls(tmp_path: Path) -> None:
    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "review-helper",
                "SessionId": "ses-shared-controls",
                "InvocationId": "run-shared-controls",
                "ApiFormat": "responses",
                "ResponsesInput": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "执行审批测试"}],
                    }
                ],
                "Metadata": {
                    "agentengine": {
                        "tool_approval_mode": "ask",
                        "collaboration_mode": "plan",
                        "goal_objective": "验证共享会话控制",
                    }
                },
                "ModelOptions": {"reasoning_effort": "high"},
            },
        ) as response:
            assert response.status_code == 200
            assert "response.completed" in "".join(response.iter_text())

    request = runtime_fixture.start_requests[0]
    assert request.config["sandbox"] == "workspace-write"
    assert request.config["approval_mode"] == "manual"
    assert request.config["tool_approval_mode"] == "ask"
    assert request.config["collaboration_mode"] == "plan"
    assert request.config["goal_objective"] == "验证共享会话控制"
    assert request.config["effort"] == "high"


def test_openai_responses_forwards_plan_goal_and_structured_attachments(
    tmp_path: Path,
) -> None:
    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "glm-5.2",
                "reasoning": {"effort": "high"},
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "分析这张图"},
                            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                        ],
                    }
                ],
                "metadata": {
                    "agent_id": "review-helper",
                    "session_id": "ses-plan-goal",
                    "collaboration_mode": "plan",
                    "goal_objective": "完成视觉回归",
                },
            },
        )

    assert response.status_code == 200
    request = runtime_fixture.start_requests[0]
    assert request.config["collaboration_mode"] == "plan"
    assert request.config["goal_objective"] == "完成视觉回归"
    assert request.config["effort"] == "high"
    assert request.input == [
        {"type": "text", "text": "分析这张图"},
        {"type": "image", "url": "data:image/png;base64,AAAA"},
    ]


def test_conversation_input_attachment_ref_reaches_the_bound_codex_runtime(
    tmp_path: Path,
) -> None:
    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        uploaded = client.post(
            "/api/v1/conversation-attachments",
            files={"file": ("notes.md", b"verified attachment", "text/markdown")},
        )
        assert uploaded.status_code == 201, uploaded.text
        attachment = uploaded.json()
        surface = client.get(
            "/api/v1/agents/review-helper/conversation-surface",
            params={"sessionId": "ses-conversation-attachment"},
        )
        assert surface.status_code == 200, surface.text
        build_id = surface.json()["buildId"]
        with client.stream(
            "POST",
            f"/api/v1/builds/{build_id}/conversation:stream",
            headers={"Idempotency-Key": "conversation-attachment-turn"},
            json={
                "input": {
                    "inputId": "input-attachment",
                    "sessionId": "ses-conversation-attachment",
                    "idempotencyKey": "conversation-attachment-turn",
                    "parts": [
                        {"kind": "text", "text": "请检查附件"},
                        {
                            "kind": "attachment",
                            "attachmentRef": attachment["attachmentRef"],
                            "mediaType": attachment["mediaType"],
                            "name": attachment["name"],
                        },
                    ],
                    "modelRef": "glm-5.2",
                    "reasoning": "high",
                    "extensions": {
                        "ksadk.approval": "risk",
                        "ksadk.collaboration": "plan",
                        "ksadk.goal": "检查附件并形成计划",
                    },
                }
            },
        ) as response:
            stream = "".join(response.iter_text())

    assert response.status_code == 200, stream
    request = runtime_fixture.start_requests[0]
    assert request.config["effort"] == "high"
    assert request.config["tool_approval_mode"] == "risk"
    assert request.config["collaboration_mode"] == "plan"
    assert request.config["goal_objective"] == "检查附件并形成计划"
    assert request.input[0] == {"type": "text", "text": "请检查附件"}
    assert request.input[1]["type"] == "input_file"
    assert request.input[1]["filename"] == "notes.md"
    assert request.input[1]["file_data"].startswith("data:text/markdown;base64,")


def test_openai_responses_rejects_unknown_collaboration_mode(tmp_path: Path) -> None:
    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "input": "不应执行",
                "metadata": {
                    "agent_id": "review-helper",
                    "collaboration_mode": "creative-ish",
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "COLLABORATION_MODE_INVALID"
    assert runtime_fixture.start_requests == []


def test_openai_responses_rejects_unknown_approval_mode(tmp_path: Path) -> None:
    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "input": "无效批准模式不应静默降级",
                "metadata": {
                    "agent_id": "review-helper",
                    "approval_mode": "unrestricted-ish",
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "APPROVAL_MODE_INVALID"
    assert runtime_fixture.start_requests == []


def test_openai_responses_rejects_unbound_model_before_starting_codex(
    tmp_path: Path,
) -> None:
    """Break caught: the standard endpoint bypasses the Agent model allowlist."""

    runtime_fixture = RuntimeFixture(standard_codex_events)
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=runtime_fixture.executor,
    )
    service.save_codex_manifest(CodexAgentManifest.model_validate(_manifest()))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "unbound-model",
                "input": "不应执行",
                "metadata": {"agent_id": "review-helper"},
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "MODEL_NOT_BOUND"
        assert runtime_fixture.start_requests == []


def test_delete_session_removes_persisted_runs_and_cannot_succeed_twice(
    tmp_path: Path,
) -> None:
    """Break caught: deleting a sidebar row without deleting its persisted runs."""

    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        client.put("/api/v1/codex/manifest", json=_manifest())
        build_operation = client.post(
            "/api/v1/codex/builds",
            headers={"Idempotency-Key": "delete-session-build"},
        ).json()
        build_id = _wait(client, build_operation["id"])["resourceId"]
        run_operation = client.post(
            f"/api/v1/codex/builds/{build_id}/runs",
            headers={"Idempotency-Key": "delete-session-run"},
            json={"sessionId": "ses-delete-me", "input": {"content": "review"}},
        ).json()
        _wait(client, run_operation["id"])

        deleted = client.delete("/api/v1/sessions/ses-delete-me")

        assert deleted.status_code == 204
        assert client.get("/api/v1/runs?sessionId=ses-delete-me").json()["items"] == []
        assert not list((tmp_path / ".agentkit/runs").glob("run_*.json"))
        second_delete = client.delete("/api/v1/sessions/ses-delete-me")
        assert second_delete.status_code == 404


def test_codex_agent_can_be_edited_then_recoverably_deleted_with_local_state(
    tmp_path: Path,
) -> None:
    """Break caught: Codex Agent detail was read-only and delete always returned 422."""

    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    def payload(agent_id: str, prompt: str) -> dict:
        return {
            "id": agent_id,
            "name": agent_id,
            "description": prompt,
            "template": "blank",
            "spec": {
                "runtime": {"type": "codex", "version": "0.144.4"},
                "description": prompt,
                "instructions": {"system": prompt, "task": ""},
                "bindings": {},
            },
        }

    with TestClient(app) as client:
        client.post(
            "/api/v1/agents",
            json=payload("review-helper", "保留这个 Agent。"),
        )
        created = client.post(
            "/api/v1/agents",
            json=payload("delete-helper", "删除前的系统提示词。"),
        )
        assert created.status_code == 201

        detail = client.get("/api/v1/agents/delete-helper").json()
        edited_spec = detail["draft"]["spec"]
        edited_spec["instructions"]["system"] = "编辑后的系统提示词。"
        edited = client.put(
            "/api/v1/agents/delete-helper",
            headers={"If-Match": "1"},
            json=edited_spec,
        )
        assert edited.status_code == 200
        assert edited.json()["spec"]["instructions"]["system"] == "编辑后的系统提示词。"
        assert "编辑后的系统提示词" in (
            tmp_path / "agents/delete-helper/agentengine.yaml"
        ).read_text(encoding="utf-8")

        build_operation = client.post(
            "/api/v1/agents/delete-helper/builds",
            headers={"Idempotency-Key": "delete-agent-build"},
            json={"revision": 2, "runEvaluation": False},
        ).json()
        build_id = _wait(client, build_operation["id"])["resourceId"]
        build = client.get(f"/api/v1/builds/{build_id}").json()
        artifact = tmp_path / build["artifactPath"]
        assert artifact.is_file()

        run_operation = client.post(
            f"/api/v1/builds/{build_id}/runs",
            headers={"Idempotency-Key": "delete-agent-run"},
            json={
                "sessionId": "ses-delete-agent",
                "input": {"content": "运行后再删除"},
            },
        ).json()
        run_id = _wait(client, run_operation["id"])["resourceId"]
        trace_id = client.get(f"/api/v1/runs/{run_id}").json()["traceId"]

        deleted = client.delete("/api/v1/agents/delete-helper")

        assert deleted.status_code == 204
        assert [
            item["metadata"]["id"] for item in client.get("/api/v1/agents").json()["items"]
        ] == ["review-helper"]
        assert client.get("/api/v1/agents/delete-helper").status_code == 404
        assert client.get(f"/api/v1/builds/{build_id}").status_code == 404
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 404
        assert client.get(f"/api/v1/traces/{trace_id}").status_code == 404
        assert not artifact.exists()
        assert not (tmp_path / "agents/delete-helper").exists()
        trash = list((tmp_path / ".agentkit/trash/agents").glob("delete-helper-*"))
        assert len(trash) == 1
        assert list(trash[0].rglob("agentengine.yaml"))
        assert list(trash[0].rglob(f"{build_id}.json"))
        assert list(trash[0].rglob(f"{run_id}.json"))
        declarations = list(trash[0].rglob("*-runtime.yaml"))
        assert len(declarations) == 1
        assert declarations[0].with_suffix(".lock.json").is_file()
        assert not list(trash[0].rglob("*.zip"))
        assert client.delete("/api/v1/agents/delete-helper").status_code == 404


def test_codex_agent_preserves_display_metadata_and_real_revision(tmp_path: Path) -> None:
    """ManagedRuntime YAML stays narrow while Studio metadata remains editable."""

    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents",
            json={
                "id": "review-helper",
                "name": "团队审查助手",
                "description": "保留在 Studio 元数据中",
                "spec": {
                    "runtime": {"type": "codex", "version": "0.144.4"},
                    "description": "保留在 Studio 元数据中",
                    "instructions": {"system": "执行代码审查。", "task": ""},
                    "bindings": {},
                },
            },
        )
        assert created.status_code == 201
        assert created.json()["metadata"]["name"] == "团队审查助手"
        assert created.json()["metadata"]["revision"] == 1

        spec = created.json()["spec"]
        spec["instructions"]["system"] = "执行更严格的代码审查。"
        updated = client.put(
            "/api/v1/agents/review-helper",
            headers={"If-Match": "1"},
            json=spec,
        )
        assert updated.status_code == 200
        assert updated.json()["metadata"]["name"] == "团队审查助手"
        assert updated.json()["metadata"]["revision"] == 2
        assert client.get("/api/v1/agents/review-helper").json()["draft"] == updated.json()

        stale = client.put(
            "/api/v1/agents/review-helper",
            headers={"If-Match": "1"},
            json=spec,
        )
        assert stale.status_code == 409


def test_codex_agent_appearance_is_kept_in_studio_sidecar(tmp_path: Path) -> None:
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents",
            json={
                "id": "avatar-codex",
                "name": "Avatar Codex",
                "spec": {
                    "runtime": {"type": "codex", "version": "0.144.4"},
                    "instructions": {"system": "Review code.", "task": ""},
                    "bindings": {},
                },
            },
        ).json()
        updated = client.put(
            "/api/v1/agents/avatar-codex/appearance",
            headers={"If-Match": str(created["metadata"]["revision"])},
            json={"icon": "code", "color": "#7c5cc4", "imageUrl": None},
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["metadata"]["appearance"] == {
            "icon": "code",
            "color": "#7c5cc4",
            "imageUrl": None,
        }
        persisted = client.get("/api/v1/agents/avatar-codex").json()["draft"]
        assert persisted["metadata"]["appearance"] == updated.json()["metadata"]["appearance"]
        assert service.codex_manifests.load("avatar-codex").manifest.name == "avatar-codex"


def test_codex_rejects_ksadk_tool_binding_instead_of_pretending_to_execute(
    tmp_path: Path,
) -> None:
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents",
            json={
                "id": "review-helper",
                "name": "Review Helper",
                "spec": {
                    "runtime": {"type": "codex", "version": "0.144.4"},
                    "instructions": {"system": "Review code.", "task": ""},
                    "bindings": {},
                },
            },
        ).json()
        tool = next(
            item
            for item in client.get("/api/v1/catalog/resources?kind=tool").json()["items"]
            if item["name"] == "component_status"
        )
        rejected = client.put(
            "/api/v1/agents/review-helper/bindings",
            headers={"If-Match": str(created["metadata"]["revision"])},
            json={"tools": [{"resourceId": tool["resourceId"]}]},
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "TOOL_RUNTIME_INCOMPATIBLE"


def test_codex_update_preserves_dormant_historical_tool_but_rejects_changes(
    tmp_path: Path,
) -> None:
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents",
            json={
                "id": "legacy-tool-codex",
                "name": "Legacy Tool Codex",
                "spec": {
                    "runtime": {"type": "codex", "version": "0.144.4"},
                    "instructions": {"system": "Old prompt.", "task": ""},
                    "bindings": {},
                },
            },
        ).json()
        historical = service.codex_drafts.get("legacy-tool-codex")
        assert historical is not None
        historical.spec.bindings.tools = [
            CapabilityBinding(
                resource_id="tool-legacy",
                approval="policy",
                config={"mode": "safe"},
            )
        ]
        service.codex_drafts.save(historical)

        detail = client.get("/api/v1/agents/legacy-tool-codex").json()["draft"]
        detail["spec"]["instructions"]["system"] = "Updated prompt."
        updated = client.put(
            "/api/v1/agents/legacy-tool-codex",
            headers={"If-Match": str(created["metadata"]["revision"])},
            json=detail["spec"],
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["metadata"]["revision"] == created["metadata"]["revision"] + 1
        assert updated.json()["spec"]["bindings"]["tools"] == [
            {
                "resourceId": "tool-legacy",
                "enabled": True,
                "approval": "policy",
                "config": {"mode": "safe"},
            }
        ]

        changed_spec = updated.json()["spec"]
        changed_spec["bindings"]["tools"][0]["resourceId"] = "tool-changed"
        rejected = client.put(
            "/api/v1/agents/legacy-tool-codex",
            headers={"If-Match": str(updated.json()["metadata"]["revision"])},
            json=changed_spec,
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "TOOL_RUNTIME_INCOMPATIBLE"


def test_codex_yaml_first_agent_projects_and_losslessly_updates_model_skill_and_mcp(
    tmp_path: Path,
) -> None:
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    model_primary = service.catalog.create_model_profile(
        name="codex-primary",
        display_name="Codex Primary",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            model="codex-primary",
            endpoint_url="https://models.example.test/v1/chat/completions",
            credential_ref="env://MODEL_KEY",
        ),
    )
    model_fallback = service.catalog.create_model_profile(
        name="codex-fallback",
        display_name="Codex Fallback",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            model="codex-fallback",
            endpoint_url="https://models.example.test/v1/chat/completions",
            credential_ref="env://MODEL_KEY",
        ),
    )
    skill = service.catalog.import_skill_zip(_skill_archive(), filename="review.zip")
    mcp = service.catalog.create_mcp_server(
        display_name="Review MCP",
        description="",
        server=MCPServerRef(
            name="review-mcp",
            version="1.0.0",
            transport="http",
            endpoint_url="https://mcp.example.test/rpc",
        ),
    )
    service.codex_manifests.save(
        CodexAgentManifest.model_validate(
            {
                **_manifest("Review the workspace carefully.\n"),
                "model": "codex-primary",
                "models": ["codex-primary", "codex-fallback"],
                "skills": [skill.resource_id],
                "mcp_servers": [
                    {"name": "review-mcp", "url": "https://mcp.example.test/rpc"},
                    {
                        "name": "legacy-private",
                        "url": "https://legacy.example.test/rpc",
                        "custom": {"keep": True},
                    },
                ],
            }
        )
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        detail = client.get("/api/v1/agents/review-helper").json()
        bindings = detail["draft"]["spec"]["bindings"]
        assert bindings["modelProfileId"] == model_primary.resource_id
        assert bindings["modelProfileIds"] == [
            model_primary.resource_id,
            model_fallback.resource_id,
        ]
        assert [item["resourceId"] for item in bindings["skills"]] == [skill.resource_id]
        assert [item["resourceId"] for item in bindings["mcpServers"]] == [mcp.resource_id]
        assert detail["bindingProjection"]["unresolvedMcpServers"] == [
            {"name": "legacy-private", "reason": "not-in-resource-catalog"},
        ]

        updated_spec = detail["draft"]["spec"]
        updated_spec["instructions"]["system"] = "Review and summarize the workspace."
        updated = client.put(
            "/api/v1/agents/review-helper?name=Review+Helper",
            headers={"If-Match": str(detail["draft"]["metadata"]["revision"])},
            json=updated_spec,
        )
        assert updated.status_code == 200, updated.text

    saved = yaml.safe_load((tmp_path / "agentengine.yaml").read_text(encoding="utf-8"))
    assert saved["model"] == "codex-primary"
    assert saved["models"] == ["codex-primary", "codex-fallback"]
    assert saved["skills"] == [skill.resource_id]
    assert saved["mcp_servers"] == [
        {"name": "review-mcp", "url": "https://mcp.example.test/rpc"},
        {
            "name": "legacy-private",
            "url": "https://legacy.example.test/rpc",
            "custom": {"keep": True},
        },
    ]
