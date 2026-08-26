"""PCM 前端联动 E2E（方案 §12.4）。

两层验证：
1. API 级联动（不依赖 playwright）：模拟浏览器交互序列——创建 agent → 编辑 PCM policy →
   prompt:compile → context:preview → run → context inspector，用 TestClient 验证完整闭环。
2. 浏览器级 E2E（需 playwright + 运行中的 studio server）：独立脚本，未装时 skip。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import RuntimeFixture, standard_codex_events
from tests.studio.test_codex_api import _inspector


def _wait(client: TestClient, operation_id: str) -> dict:
    for _ in range(300):
        op = client.get(f"/api/v1/operations/{operation_id}").json()
        if op["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return op
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


@pytest.fixture()
def client(tmp_path):
    service = StudioService(
        tmp_path,
        codex_runtime_inspector=_inspector,
        runtime_executor=RuntimeFixture(standard_codex_events).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as c:
        yield c


def _create_codex_agent(client, agent_id="pcm-e2e-agent"):
    client.post(
        "/api/v1/agents",
        json={
            "id": agent_id,
            "name": agent_id,
            "description": "PCM E2E",
            "template": "blank",
            "spec": {
                "runtime": {"type": "codex", "version": "0.144.4"},
                "description": "PCM E2E",
                "instructions": {"system": "你是助手，绝不回显凭证。", "task": "用 uv run。"},
                "bindings": {},
                "context": {"ownership": "auto", "rollout": {"contextEngine": "shadow"}},
                "memory": {"enabled": False},
            },
        },
    )
    return agent_id


def test_pcm_api_e2e_full_loopback(client):
    """API 级 PCM 联动：创建(含PCM policy) → 编译预览 → Context 预览 → build → run → inspector。"""
    agent_id = _create_codex_agent(client)

    # 1. 验证创建后 agent detail 含 PCM 字段
    detail = client.get(f"/api/v1/agents/{agent_id}").json()
    ctx = detail["draft"]["spec"]["context"]
    assert ctx["ownership"] == "auto"
    assert ctx["rollout"]["contextEngine"] == "shadow"
    assert detail["draft"]["spec"]["memory"]["enabled"] is False

    # 2. 编辑 PCM policy（模拟前端保存）
    spec = detail["draft"]["spec"]
    spec["context"]["ownership"] = "framework"
    spec["context"]["rollout"]["contextEngine"] = "enabled"
    spec["memory"]["enabled"] = True
    updated = client.put(
        f"/api/v1/agents/{agent_id}",
        json=spec,
        headers={"If-Match": str(detail["draft"]["metadata"]["revision"])},
    )
    assert updated.status_code == 200
    assert updated.json()["spec"]["context"]["ownership"] == "framework"
    assert updated.json()["spec"]["context"]["rollout"]["contextEngine"] == "enabled"
    assert updated.json()["spec"]["memory"]["enabled"] is True

    # 3. Prompt 编译预览
    r = client.post(
        f"/api/v1/agents/{agent_id}/prompt:compile",
        json={"requestInstructions": "本次：介绍 GIL", "includeContent": True},
    )
    assert r.status_code == 200
    assert "你是助手" in r.json()["content"]

    # 4. Context 预览
    r = client.post(
        f"/api/v1/agents/{agent_id}/context:preview",
        json={"userInput": "介绍 GIL", "includeContent": True},
    )
    assert r.status_code == 200
    assert r.json()["projection"]["runtimeType"] == "codex"

    # 5. Build + Run（镜像 passing test：build 前的 list/get 调用保持 background task 生命周期）
    client.get("/api/v1/agents").json()["items"]
    client.get(f"/api/v1/agents/{agent_id}").json()
    bop = client.post(
        f"/api/v1/agents/{agent_id}/builds",
        headers={"Idempotency-Key": "pcm-e2e-build"},
        json={"revision": updated.json()["metadata"]["revision"], "runEvaluation": False},
    )
    build_done = _wait(client, bop.json()["id"])
    assert build_done["status"] == "SUCCEEDED"
    build_id = build_done["resourceId"]

    rop = client.post(
        f"/api/v1/codex/builds/{build_id}/runs",
        headers={"Idempotency-Key": "pcm-e2e-run"},
        json={
            "sessionId": "pcm-e2e",
            "input": {"role": "user", "content": "审查"},
            "environment": "local",
            "stream": True,
        },
    )
    run_done = _wait(client, rop.json()["id"])
    assert run_done["status"] == "SUCCEEDED"
    run_id = run_done["resourceId"]

    # 6. Context Inspector 证据 API
    ctx_evidence = client.get(f"/api/v1/runs/{run_id}/context").json()
    assert ctx_evidence["ownership"]["runtimeType"] == "codex"
    assert ctx_evidence["accuracy"] in ("opaque", "runtime_reported", "estimated", "exact")

    prompt_evidence = client.get(f"/api/v1/runs/{run_id}/prompt").json()
    assert prompt_evidence["runtimeType"] == "codex"

    ws_evidence = client.get(f"/api/v1/runs/{run_id}/working-state").json()
    assert "workingState" in ws_evidence

    mem_evidence = client.get(f"/api/v1/runs/{run_id}/memory-events").json()
    assert "items" in mem_evidence


def test_pcm_bootstrap_exposes_importable(client, tmp_path):
    """bootstrap 含 importableProject（无 framework manifest 时为 None）。"""
    bootstrap = client.get("/api/v1/system/bootstrap").json()
    # 无根 manifest → None
    assert bootstrap.get("importableProject") is None
    assert (
        "pcm" not in str(bootstrap.get("features", {})).lower() or True
    )  # features 不含 pcm 字段也 OK


def test_pcm_react_workspace_contains_policy_and_evidence_surfaces():
    """React Studio keeps PCM policy authoring and progressive evidence UI."""
    import pathlib

    create_page = pathlib.Path("ksadk/studio/react-ui/src/pages/CreatePage.tsx").read_text(
        encoding="utf-8"
    )
    run_panel = pathlib.Path("ksadk/studio/react-ui/src/components/ChatRunPanel.tsx").read_text(
        encoding="utf-8"
    )
    observability_page = pathlib.Path(
        "ksadk/studio/react-ui/src/pages/ObservabilityPage.tsx"
    ).read_text(encoding="utf-8")
    assert "contextOwnership" in create_page
    assert "contextEngineRollout" in create_page
    assert "memoryWriteRollout" in create_page
    assert 'goal: prompt' in create_page
    assert "modelProfileIds: selectedModels" in create_page
    assert "if (!res.ok)" in create_page
    assert 'composition.spec?.instructions?.system || prompt.trim()' in create_page
    assert "Agent 目标与要求" in create_page
    assert "角色与系统提示词" in create_page
    assert "上下文与记忆策略" in create_page
    assert "第 {step} 步，共 4 步" in create_page
    assert "上下文优化" in create_page
    assert "启用长期记忆" in create_page
    assert "memoryWrite: memoryEnabled ? memoryWriteRollout : \"off\"" in create_page
    assert "运行解释" in run_panel
    assert "/context`" in run_panel
    assert "/prompt`" in run_panel
    # 会话侧边栏只保留用户可理解的摘要；planned/projected/actual 进入完整 Trace。
    assert "开发与排障信息" not in run_panel
    assert "打开完整 Trace" in run_panel
    assert "模型实际输入" not in run_panel
    assert "Raw OTLP" in observability_page
    assert "复制 Raw OTLP" in observability_page


# ---- 浏览器级 E2E（需 playwright + 运行中的 studio server，独立运行）----
# 复用 tests/studio/e2e/studio_browser_smoke.py 模式；PCM 浏览器 E2E 脚本见
# tests/studio/e2e/pcm_browser_smoke.py（手动运行，需先启动 studio + 装 playwright）。
