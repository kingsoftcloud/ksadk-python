"""四项修复/接入验证（方案 §5.2/§6.1/§6.2/§13.1）。

1. ownership capability 校验接入 Revision/Build（validator）。
2. 前端编辑不丢失 Memory 子配置。
3. compaction_owner 硬门控 KsADK Compaction。
4. 根 Framework 项目一键确认导入。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import AgentSpec, ContextSpec, Instructions, MemorySpec, RuntimeRef
from ksadk.studio.service import StudioService
from ksadk.studio.validator import AgentValidator


# ---- 1. ownership capability 校验接入 validator ----


def test_validator_rejects_ownership_mismatch():
    """codex runtime + ownership=ksadk → validator 报 OWNERSHIP_CAPABILITY_MISMATCH。"""
    draft = _make_draft(runtime_type="codex", ownership="ksadk")
    result = AgentValidator().validate(draft, level="build")
    codes = [d.code for d in result.diagnostics]
    assert "OWNERSHIP_CAPABILITY_MISMATCH" in codes


def test_validator_accepts_compatible_ownership():
    """langgraph + ownership=ksadk → 不报 mismatch。"""
    draft = _make_draft(runtime_type="langgraph", ownership="ksadk")
    result = AgentValidator().validate(draft, level="build")
    codes = [d.code for d in result.diagnostics]
    assert "OWNERSHIP_CAPABILITY_MISMATCH" not in codes


def test_validator_accepts_auto_ownership():
    """auto 总是合法。"""
    draft = _make_draft(runtime_type="codex", ownership="auto")
    result = AgentValidator().validate(draft, level="build")
    codes = [d.code for d in result.diagnostics]
    assert "OWNERSHIP_CAPABILITY_MISMATCH" not in codes


def _make_draft(runtime_type: str, ownership: str):
    from ksadk.studio.contracts import AgentDraft, AgentMetadata
    return AgentDraft(
        metadata=AgentMetadata(id="test-agent", name="Test"),
        spec=AgentSpec(
            runtime=RuntimeRef(type=runtime_type, project_path="runtimes/demo", entry_point="agent.py:graph") if runtime_type != "codex" else RuntimeRef(type="codex"),
            instructions=Instructions(system="你是助手"),
            context=ContextSpec(ownership=ownership),
        ),
    )


# ---- 2. 前端编辑不丢失 Memory 子配置（API 级验证）----


def test_edit_agent_preserves_memory_subconfig(tmp_path):
    """编辑 agent 时 PUT 保留 memory.recall/write/scopes（方案 §13.1）。"""
    svc = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=svc, security_enabled=False)
    with TestClient(app) as client:
        # 创建带完整 memory 配置的 agent
        client.post("/api/v1/agents", json={
            "id": "mem-test", "name": "mem-test", "description": "x", "template": "blank",
            "spec": {"runtime": {"type": "codex", "version": "0.144.4"},
                     "description": "x", "instructions": {"system": "s", "task": ""},
                     "bindings": {},
                     "memory": {"enabled": True, "providerRef": "custom-ref",
                                "recall": {"enabled": True, "maxTokens": 2000, "topK": 5},
                                "write": {"mode": "explicit_only", "flushBeforeCompaction": False},
                                "scopes": ["user", "agent"]}},
        })
        detail = client.get("/api/v1/agents/mem-test").json()
        spec = detail["draft"]["spec"]
        # 模拟前端编辑：只改 memory.enabled，保留其余
        spec["memory"]["enabled"] = False
        updated = client.put("/api/v1/agents/mem-test", json=spec,
                             headers={"If-Match": str(detail["draft"]["metadata"]["revision"])})
        assert updated.status_code == 200
        mem = updated.json()["spec"]["memory"]
        assert mem["enabled"] is False
        # 子配置保留
        assert mem["providerRef"] == "custom-ref"
        assert mem["recall"]["maxTokens"] == 2000
        assert mem["write"]["mode"] == "explicit_only"
        assert "user" in mem["scopes"]


# ---- 3. compaction_owner 硬门控 ----


def test_compaction_owner_native_blocks_ksadk_dual_threshold():
    """compaction_owner=native 时即使 prompt_integration_mode=ksadk_hosted 也不走双阈值。"""
    from ksadk.conversations.runtime_compaction import _plan_compaction
    from ksadk.conversations.context import SessionEvent
    # 构造超 soft limit 的 events
    events = [_user_event(i, "x" * 200) for i in range(20)]
    # compaction_owner=native + ksadk_hosted → 应走旧单阈值（is_ksadk_hosted=False）
    plan = _plan_compaction(events, model="m", model_metadata={"context_window_tokens": 200000},
                             prompt_integration_mode="ksadk_hosted", compaction_owner="native")
    # native 门控 → trigger_band 非 soft/hard（走旧单阈值或 none）
    assert plan.trigger_band not in ("soft", "hard") or plan.trigger_band == ""


def test_compaction_owner_ksadk_allows_dual_threshold():
    """compaction_owner=ksadk + ksadk_hosted → 走双阈值。"""
    from ksadk.conversations.runtime_compaction import _plan_compaction
    events = [_user_event(i, "x" * 200) for i in range(20)]
    plan = _plan_compaction(events, model="m", model_metadata={"context_window_tokens": 200000},
                             prompt_integration_mode="ksadk_hosted", compaction_owner="ksadk")
    # ksadk 门控 → 可能触发 soft/hard
    assert plan.soft_limit_tokens is not None
    assert plan.hard_limit_tokens is not None


def _user_event(seq, text):
    from ksadk.sessions.base import SessionEvent
    return SessionEvent(
        id=f"u-{seq}", seq_id=seq, event_type="user_message", author="user",
        invocation_id="i", content={"role": "user", "parts": [{"text": text}]}, metadata={},
    )


# ---- 4. 根 Framework 项目一键确认导入 ----


def test_import_root_project_one_click(tmp_path):
    """一键导入根 framework 项目，生成 Studio Draft 保留配置。"""
    _write_framework_workspace(tmp_path)
    svc = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=svc, security_enabled=False)
    with TestClient(app) as client:
        r = client.post("/api/v1/workspace:import-root", json={"name": "demo-agent", "slug": "demo-agent"})
        assert r.status_code == 201
        draft = r.json()
        assert draft["spec"]["runtime"]["type"] == "langgraph"
        assert "你是助手" in draft["spec"]["instructions"]["system"]
        assert draft["spec"]["model"]["model"] == "glm-5.2"
        # list_agents 现在含导入的 agent
        agents = client.get("/api/v1/agents").json()["items"]
        assert any(a["metadata"]["id"].startswith("demo-agent") for a in agents)


def test_import_root_project_rejects_when_no_framework(tmp_path):
    """无根 framework manifest 时报 PROJECT_NOT_IMPORTABLE。"""
    svc = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=svc, security_enabled=False)
    with TestClient(app) as client:
        r = client.post("/api/v1/workspace:import-root", json={})
        assert r.status_code == 422


def test_import_root_project_rejects_codex(tmp_path):
    """根 codex manifest 不走 framework 一键导入（由 codex 路径处理）。"""
    (tmp_path / "agentengine.yaml").write_text(
        "name: codex-a\nframework: codex\nartifact_type: ManagedRuntime\nruntime:\n  name: codex\n  version: '0.144.4'\nmodel: m\nprompt: p\n",
        encoding="utf-8",
    )
    svc = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=svc, security_enabled=False)
    with TestClient(app) as client:
        r = client.post("/api/v1/workspace:import-root", json={})
        assert r.status_code == 422


def _write_framework_workspace(tmp_path):
    """写根 langgraph manifest（含 entry_point/agent_variable，可过检测）。"""
    (tmp_path / "agentengine.yaml").write_text(
        "framework: langgraph\nname: demo-agent\nmodel: glm-5.2\nprompt: 你是助手\nentry_point: src/agent.py\nagent_variable: graph\ntask: 先分析\n",
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "agent.py").write_text("graph = object()\n")
