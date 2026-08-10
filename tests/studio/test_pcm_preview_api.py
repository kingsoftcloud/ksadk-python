"""PR-S2：Prompt compile + Context preview API 集成测试（方案 §6.2）。

验证只读预览：复用真实 PromptCompiler/Planner，不写 Session/Trace/Build，不调模型，
默认不返回敏感正文。复用 studio test fixture 模式。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService


def _client(tmp_path: Path) -> tuple[TestClient, StudioService]:
    service = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    return TestClient(app), service


def _create_langgraph_agent(client: TestClient, agent_id: str = "demo-agent") -> None:
    client.post(
        "/api/v1/agents",
        json={
            "id": agent_id,
            "name": agent_id,
            "description": "demo",
            "template": "blank",
            "spec": {
                "runtime": {
                    "type": "langgraph",
                    "projectPath": "runtimes/demo",
                    "entryPoint": "src/agent.py:graph",
                },
                "instructions": {"system": "你是 Python 助手，绝不回显凭证。", "task": "用 uv run。"},
                "bindings": {},
            },
        },
    )


def test_prompt_compile_returns_hashes_without_content(tmp_path):
    c, _ = _client(tmp_path)
    _create_langgraph_agent(c)
    r = c.post("/api/v1/agents/demo-agent/prompt:compile", json={"requestInstructions": "本次：介绍 GIL"})
    assert r.status_code == 200
    body = r.json()
    assert body["contentHash"].startswith("sha256:")
    assert body["stablePrefixHash"].startswith("sha256:")
    assert body["sectionCount"] >= 1
    assert body["runtimeType"] == "langgraph"
    # 默认不返回敏感正文
    assert "content" not in body


def test_prompt_compile_include_content_returns_canonical(tmp_path):
    c, _ = _client(tmp_path)
    _create_langgraph_agent(c)
    r = c.post(
        "/api/v1/agents/demo-agent/prompt:compile",
        json={"requestInstructions": "介绍 GIL", "includeContent": True},
    )
    body = r.json()
    assert "content" in body
    assert "你是 Python 助手" in body["content"]
    assert "绝不回显凭证" in body["content"]  # agent_system 进编译


def test_prompt_compile_is_readonly_no_session_written(tmp_path):
    c, svc = _client(tmp_path)
    _create_langgraph_agent(c)
    c.post("/api/v1/agents/demo-agent/prompt:compile", json={"requestInstructions": "x"})
    # preview 不应产生 run/trace
    runs = svc.event_store.list_runs()
    assert runs == []


def test_context_preview_returns_budget_and_items(tmp_path):
    c, _ = _client(tmp_path)
    _create_langgraph_agent(c)
    r = c.post(
        "/api/v1/agents/demo-agent/context:preview",
        json={
            "userInput": "用一句话介绍 GIL。",
            "requestInstructions": "本次：介绍 GIL",
            "simulatedHistory": [{"role": "user", "content": "你好"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accuracy"] in ("estimated", "runtime_reported", "exact", "opaque")
    assert body["projection"]["runtimeType"] == "langgraph"
    assert body["projection"]["integrationMode"] == "framework_assisted"
    assert "budget" in body and body["budget"]["maxInputTokens"] > 0
    assert isinstance(body["items"], list)
    assert "decisions" in body
    assert "totalsByKind" in body


def test_context_preview_no_content_by_default(tmp_path):
    c, _ = _client(tmp_path)
    _create_langgraph_agent(c)
    r = c.post(
        "/api/v1/agents/demo-agent/context:preview",
        json={"userInput": "hi", "includeContent": False},
    )
    body = r.json()
    assert "system" not in body


def test_context_preview_include_content(tmp_path):
    c, _ = _client(tmp_path)
    _create_langgraph_agent(c)
    r = c.post(
        "/api/v1/agents/demo-agent/context:preview",
        json={"userInput": "hi", "includeContent": True},
    )
    body = r.json()
    assert "system" in body
    assert "你是 Python 助手" in body["system"]


def test_context_preview_is_readonly_no_run_written(tmp_path):
    c, svc = _client(tmp_path)
    _create_langgraph_agent(c)
    c.post("/api/v1/agents/demo-agent/context:preview", json={"userInput": "hi"})
    assert svc.event_store.list_runs() == []


def test_prompt_compile_codex_native_runtime(tmp_path):
    c, _ = _client(tmp_path)
    # codex agent 经根 manifest 创建
    c.put(
        "/api/v1/codex/manifest",
        json={
            "name": "codex-demo",
            "version": "1.0.0",
            "framework": "codex",
            "artifact_type": "ManagedRuntime",
            "runtime": {"name": "codex", "version": "0.144.4"},
            "model": "m",
            "prompt": "你是 codex 助手。",
        },
    )
    r = c.post("/api/v1/agents/codex-demo/prompt:compile", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["runtimeType"] == "codex"
    assert body["contentHash"].startswith("sha256:")
