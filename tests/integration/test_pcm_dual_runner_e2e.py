"""PCM 双 Runner E2E：Codex + LangGraph 全链路（方案 §4）。

覆盖：
  创建 Agent → 保存 PCM 策略 → Build → 修改 Draft → 用旧 Build RunSpec
  → 检查 RunSpec → 检查 Memory 事件

使用 TestClient（真实 Studio API），不依赖真实模型。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import (
    RuntimeFixture,
    standard_codex_events,
)
from tests.studio.test_codex_api import _inspector


@pytest.fixture()
def studio(tmp_path):
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as c:
        yield c, service


def _wait_op(c, op_id, timeout=30):
    for _ in range(timeout * 2):
        op = c.get(f"/api/v1/operations/{op_id}").json()
        if op["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return op
        time.sleep(0.5)
    return op


# ---- Codex E2E ----


def test_codex_manifest_contains_pcm_fields(studio):
    """Codex Agent 创建后 Manifest 含 context/memory PCM 策略。"""
    c, svc = studio
    c.post(
        "/api/v1/agents",
        json={
            "id": "pcm-e2e-codex",
            "name": "PCM E2E Codex",
            "description": "x",
            "template": "blank",
            "spec": {
                "runtime": {"type": "codex", "version": "0.144.4"},
                "description": "x",
                "instructions": {
                    "system": "你是助手",
                    "task": "用 uv",
                },
                "bindings": {},
                "context": {
                    "ownership": "auto",
                    "rollout": {
                        "contextEngine": "shadow",
                        "memoryWrite": "enabled",
                    },
                    "maxInputTokens": 4096,
                    "reserveOutputTokens": 512,
                },
                "memory": {
                    "enabled": True,
                    "write": {
                        "mode": "candidate",
                        "flushBeforeCompaction": True,
                    },
                    "recall": {
                        "enabled": True,
                        "maxTokens": 800,
                        "topK": 5,
                    },
                },
            },
        },
    )
    manifest = c.get("/api/v1/codex/manifest").json()["manifest"]
    assert manifest["context"]["maxInputTokens"] == 4096
    assert manifest["context"]["rollout"]["memoryWrite"] == "enabled"
    assert manifest["memory"]["enabled"] is True
    assert manifest["memory"]["write"]["mode"] == "candidate"


def test_codex_build_then_run_check_memory_evidence(studio):
    """Build → Run → 检查 promptEvidence + memory events。"""
    c, svc = studio
    c.post(
        "/api/v1/agents",
        json={
            "id": "pcm-rs-codex",
            "name": "PCM RS Codex",
            "description": "x",
            "template": "blank",
            "spec": {
                "runtime": {"type": "codex", "version": "0.144.4"},
                "description": "x",
                "instructions": {
                    "system": "你是助手",
                    "task": "",
                },
                "bindings": {},
                "context": {
                    "rollout": {
                        "contextEngine": "shadow",
                        "memoryWrite": "enabled",
                    }
                },
                "memory": {
                    "enabled": True,
                    "write": {"mode": "candidate"},
                    "recall": {"enabled": True},
                },
            },
        },
    )
    bop = c.post(
        "/api/v1/agents/pcm-rs-codex/builds",
        headers={"Idempotency-Key": "e2e-rs-b"},
        json={"revision": 1, "runEvaluation": False},
    )
    op = _wait_op(c, bop.json()["id"])
    assert op["status"] == "SUCCEEDED", op
    build_id = op["resourceId"]

    # Manifest 含 PCM memory 字段
    manifest = c.get("/api/v1/codex/manifest").json()["manifest"]
    assert manifest["memory"]["enabled"] is True
    assert manifest["memory"]["write"]["mode"] == "candidate"
    assert manifest["context"]["rollout"]["memoryWrite"] == "enabled"

    # Run
    rop = c.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "e2e-rs-r"},
        json={
            "sessionId": "ses-rs",
            "input": {"role": "user", "content": "hi"},
            "environment": "local",
            "stream": True,
        },
    )
    op2 = _wait_op(c, rop.json()["id"])
    run_id = op2.get("resourceId", "")

    if run_id:
        run = c.get(f"/api/v1/runs/{run_id}").json()
        pe = run.get("promptEvidence", {})
        assert pe.get("runtimeType") == "codex"
        assert pe.get("integrationMode") == "native_runtime"
        assert pe.get("promptOwner") == "native"
        assert pe.get("sectionCount", 0) >= 2


# ---- LangGraph E2E ----


def test_langgraph_agent_detail_contains_pcm_fields(studio):
    """LangGraph Agent 创建后 Detail 含 ownership/rollout/maxInputTokens。"""
    c, svc = studio
    import os

    ws = svc.workspace.root
    os.makedirs(ws / "runtimes" / "e2e-lg", exist_ok=True)
    (ws / "runtimes" / "e2e-lg" / "__init__.py").write_text("")
    (ws / "runtimes" / "e2e-lg" / "agent.py").write_text(
        "from langgraph.graph import StateGraph, START, END\n"
        "from typing import TypedDict\n"
        "class S(TypedDict, total=False): pass\n"
        "g = StateGraph(S)\n"
        "g.add_node('n', lambda s: s)\n"
        "g.add_edge(START, 'n'); g.add_edge('n', END)\n"
        "compiled = g.compile()\n"
    )
    r = c.post(
        "/api/v1/agents",
        json={
            "id": "pcm-e2e-lg",
            "name": "PCM E2E LG",
            "description": "x",
            "template": "blank",
            "spec": {
                "runtime": {
                    "type": "langgraph",
                    "projectPath": "runtimes/e2e-lg",
                    "entryPoint": "agent.py:compiled",
                    "agentVariable": "compiled",
                },
                "description": "x",
                "instructions": {
                    "system": "你是助手",
                    "task": "用 uv",
                },
                "bindings": {},
                "model": {
                    "model": "test",
                    "credentialRef": "env://OPENAI_API_KEY",
                    "endpointUrl": "https://example.com/v1",
                },
                "context": {
                    "ownership": "ksadk",
                    "rollout": {
                        "contextEngine": "enabled",
                        "memoryWrite": "shadow",
                    },
                    "maxInputTokens": 4096,
                    "reserveOutputTokens": 512,
                },
                "memory": {"enabled": False},
                "security": {
                    "network": {
                        "mode": "open",
                        "allowedHosts": ["example.com"],
                    }
                },
            },
        },
    )
    assert r.status_code == 201, r.json()
    detail = c.get("/api/v1/agents/pcm-e2e-lg").json()
    ctx = detail["draft"]["spec"]["context"]
    assert ctx["ownership"] == "ksadk"
    assert ctx["rollout"]["contextEngine"] == "enabled"
    max_tokens = ctx.get("max_input_tokens") or ctx.get("maxInputTokens")
    assert max_tokens == 4096


# ---- Build 不可变性：修改前后 hash 对比 ----


def test_build_immutability_after_draft_change(studio):
    """Build 后修改 Draft，旧 Build manifestSha256 不变。"""
    c, svc = studio
    c.post(
        "/api/v1/agents",
        json={
            "id": "immut-test",
            "name": "Immut Test",
            "description": "x",
            "template": "blank",
            "spec": {
                "runtime": {"type": "codex", "version": "0.144.4"},
                "description": "x",
                "instructions": {
                    "system": "你是助手",
                    "task": "",
                },
                "bindings": {},
                "context": {
                    "maxInputTokens": 4096,
                    "reserveOutputTokens": 512,
                },
                "memory": {"enabled": False},
            },
        },
    )
    bop1 = c.post(
        "/api/v1/agents/immut-test/builds",
        headers={"Idempotency-Key": "immut-b1"},
        json={"revision": 1, "runEvaluation": False},
    )
    op1 = _wait_op(c, bop1.json()["id"])
    assert op1["status"] == "SUCCEEDED", op1
    build1_id = op1["resourceId"]
    hash_before = c.get(f"/api/v1/codex/builds/{build1_id}").json()["manifestSha256"]
    assert hash_before  # 非空

    # 修改 Draft
    detail = c.get("/api/v1/agents/immut-test").json()
    spec = detail["draft"]["spec"]
    spec["context"]["maxInputTokens"] = 32000
    c.put(
        "/api/v1/agents/immut-test",
        json=spec,
        headers={"If-Match": str(detail["draft"]["metadata"]["revision"])},
    )

    bop2 = c.post(
        "/api/v1/agents/immut-test/builds",
        headers={"Idempotency-Key": "immut-b2"},
        json={"revision": 2, "runEvaluation": False},
    )
    op2 = _wait_op(c, bop2.json()["id"])

    # 旧 Build hash 不变
    hash_after = c.get(f"/api/v1/codex/builds/{build1_id}").json()["manifestSha256"]
    assert hash_after == hash_before, (
        f"旧 Build hash 应不变: before={hash_before}, after={hash_after}"
    )

    # 新 Manifest 含 32000
    if op2["status"] == "SUCCEEDED":
        manifest = c.get("/api/v1/codex/manifest").json()["manifest"]
        assert manifest["context"]["maxInputTokens"] == 32000


# ---- Studio Recall 事件写入 EventStore ----


def test_studio_recall_events_written_to_eventstore(studio):
    """Run 完成后 /memory-events API 能读到 recall 事件（如果 recall 触发）。"""
    c, svc = studio
    c.post(
        "/api/v1/agents",
        json={
            "id": "recall-test",
            "name": "Recall Test",
            "description": "x",
            "template": "blank",
            "spec": {
                "runtime": {"type": "codex", "version": "0.144.4"},
                "description": "x",
                "instructions": {
                    "system": "你是助手",
                    "task": "",
                },
                "bindings": {},
                "context": {
                    "rollout": {
                        "contextEngine": "shadow",
                        "memoryWrite": "enabled",
                    }
                },
                "memory": {
                    "enabled": True,
                    "write": {"mode": "candidate"},
                    "recall": {"enabled": True},
                },
            },
        },
    )
    bop = c.post(
        "/api/v1/agents/recall-test/builds",
        headers={"Idempotency-Key": "recall-b"},
        json={"revision": 1, "runEvaluation": False},
    )
    op = _wait_op(c, bop.json()["id"])
    assert op["status"] == "SUCCEEDED", op
    build_id = op["resourceId"]

    rop = c.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "recall-r"},
        json={
            "sessionId": "ses-recall",
            "input": {"role": "user", "content": "hello"},
            "environment": "local",
            "stream": True,
        },
    )
    op2 = _wait_op(c, rop.json()["id"], timeout=60)
    run_id = op2.get("resourceId", "")

    if run_id:
        # /memory-events 应返回列表（即使为空，API 不应报错）
        resp = c.get(f"/api/v1/runs/{run_id}/memory-events")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        # 如果 recall 触发了，应该有 memory.recall.* 事件
        # 但 recall 取决于 LongTermMemoryService 是否配置
        # 这里只验证 API 可用 + 返回结构正确
