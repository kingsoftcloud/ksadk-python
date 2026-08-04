from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import RunHandle, StartRequest
from ksadk.studio.api import RunRequest, create_studio_app
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.contracts import RunStatus
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


async def _slow_codex_events(
    request: StartRequest,
    handle: RunHandle,
):
    common = {
        "agent_id": request.agent_id or "agent",
        "user_id": request.user_id,
        "session_id": request.session_id,
        "invocation_id": handle.run_id,
    }
    yield RuntimeEvent.create(
        EventType.RUN_STARTED,
        **common,
        seq_id=1,
        payload={"status": "in_progress"},
    )
    await asyncio.sleep(0.1)
    yield RuntimeEvent.create(
        EventType.TEXT_COMPLETED,
        **common,
        seq_id=2,
        phase="final_answer",
        payload={"text": "刷新不会中断。"},
    )
    yield RuntimeEvent.create(
        EventType.RUN_COMPLETED,
        **common,
        seq_id=3,
        payload={"status": "completed"},
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
        assert bootstrap["features"]["sharedChat"] is True
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
        assert non_stream.status_code == 200
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
        assert list(trash[0].rglob("*.zip"))
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
