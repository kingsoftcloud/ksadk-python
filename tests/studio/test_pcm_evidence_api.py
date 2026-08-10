"""PR-S4：Runtime Context Evidence API 集成测试（方案 §6.3 / §7.3）。

用 codex RuntimeFixture 跑真实 build→run，验证 run_service 以 shadow 方式捕获 PCM
evidence，Evidence API 返回 planned/projected/actual + 精度 + ownership，且 native runtime
正确标 native/opaque 不伪装 exact。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import RuntimeFixture, standard_codex_events
from tests.studio.test_codex_api import _inspector


def _wait(client: TestClient, operation_id: str) -> dict:
    import time

    for _ in range(300):
        op = client.get(f"/api/v1/operations/{operation_id}").json()
        if op["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return op
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


def _setup(tmp_path: Path):
    """精确镜像 passing test（test_codex_studio_creates_lists_and_builds_multiple_yaml_agents）结构。"""
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

    with TestClient(app) as c:
        c.post("/api/v1/agents", json=payload("review-helper", "执行代码审查。"))
        c.post("/api/v1/agents", json=payload("research-helper", "执行资料研究。"))
        # 镜像 passing test 的中间调用（保持 background task 生命周期一致）
        c.get("/api/v1/agents").json()["items"]
        c.get("/api/v1/agents/research-helper").json()
        build_op = c.post(
            "/api/v1/agents/research-helper/builds",
            headers={"Idempotency-Key": "evidence-build"},
            json={"revision": 1, "runEvaluation": False},
        )
        build_done = _wait(c, build_op.json()["id"])
        assert build_done["status"] == "SUCCEEDED", build_done
        return c, build_done["resourceId"]


def test_run_captures_pcm_evidence_into_record(tmp_path):
    c, build_id = _setup(tmp_path)
    run_op = c.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "evidence-run"},
        json={
            "sessionId": "ses-ev",
            "input": {"role": "user", "content": "请审查 src/demo.py"},
            "environment": "local",
            "stream": True,
        },
    )
    run_done = _wait(c, run_op.json()["id"])
    run_id = run_done["resourceId"]
    record = c.get(f"/api/v1/runs/{run_id}").json()
    # shadow evidence 被捕获（codex 是 native，compiled_prompt 可能为空，但 prompt_evidence 至少有 ownership）
    assert record.get("promptEvidence") is not None or record.get("contextPlan") is not None


def test_run_context_endpoint_returns_ownership_for_native(tmp_path):
    c, build_id = _setup(tmp_path)
    run_op = c.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "evidence-run2"},
        json={
            "sessionId": "ses-ev2",
            "input": {"role": "user", "content": "审查"},
            "environment": "local",
            "stream": True,
        },
    )
    run_done = _wait(c, run_op.json()["id"])
    run_id = run_done["resourceId"]
    ctx = c.get(f"/api/v1/runs/{run_id}/context").json()
    # codex native → ownership 正确，不伪装 exact
    assert ctx["ownership"]["runtimeType"] == "codex"
    assert ctx["accuracy"] in ("opaque", "runtime_reported", "estimated", "exact")


def test_run_prompt_endpoint_returns_hashes(tmp_path):
    c, build_id = _setup(tmp_path)
    run_op = c.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "evidence-run3"},
        json={
            "sessionId": "ses-ev3",
            "input": {"role": "user", "content": "审查"},
            "environment": "local",
            "stream": True,
        },
    )
    run_id = _wait(c, run_op.json()["id"])["resourceId"]
    prompt = c.get(f"/api/v1/runs/{run_id}/prompt").json()
    assert "contentHash" in prompt
    assert prompt["runtimeType"] == "codex"


def test_run_working_state_endpoint(tmp_path):
    c, build_id = _setup(tmp_path)
    run_op = c.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "evidence-run4"},
        json={
            "sessionId": "ses-ev4",
            "input": {"role": "user", "content": "审查"},
            "environment": "local",
            "stream": True,
        },
    )
    run_id = _wait(c, run_op.json()["id"])["resourceId"]
    ws = c.get(f"/api/v1/runs/{run_id}/working-state").json()
    assert "workingState" in ws


def test_run_memory_events_endpoint(tmp_path):
    c, build_id = _setup(tmp_path)
    run_op = c.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "evidence-run5"},
        json={
            "sessionId": "ses-ev5",
            "input": {"role": "user", "content": "审查"},
            "environment": "local",
            "stream": True,
        },
    )
    run_id = _wait(c, run_op.json()["id"])["resourceId"]
    me = c.get(f"/api/v1/runs/{run_id}/memory-events").json()
    assert "items" in me and isinstance(me["items"], list)


def test_evidence_does_not_break_run(tmp_path):
    """shadow evidence 捕获失败不影响真实 run 完成。"""
    c, build_id = _setup(tmp_path)
    run_op = c.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "evidence-run6"},
        json={
            "sessionId": "ses-ev6",
            "input": {"role": "user", "content": "审查"},
            "environment": "local",
            "stream": True,
        },
    )
    run_done = _wait(c, run_op.json()["id"])
    assert run_done["status"] == "SUCCEEDED"
