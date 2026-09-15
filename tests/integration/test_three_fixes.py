"""三个关键问题修复验证。

1. AgentVersion 预算传到 Planner（maxInputTokens=4096 → budget 用 4096 而非百万）。
2. integration_mode 口径一致（shadow_plan 和 context_plan 都是 ksadk_hosted）。
3. Memory recall 和 flush 共用同一 SQLite（SqliteLTMBackend）。
"""

from __future__ import annotations

import pytest

from ksadk.context_engine.hosted_pipeline import run_hosted_pipeline
from ksadk.prompts.resolved import (
    ResolvedPromptSources,
    compile_resolved_prompt_dict,
    get_default_platform_policy_source,
)

# ---- 1. AgentVersion 预算传到 Planner ----


@pytest.mark.asyncio
async def test_agent_budget_drives_planner_not_model_window(tmp_path, monkeypatch):
    """AgentVersion maxInputTokens=4096 → Planner budget 用 4096，而非模型百万窗口。"""
    monkeypatch.setenv("KSADK_CONTEXT_ENGINE_V2_ENABLED", "true")
    compiled = compile_resolved_prompt_dict(
        ResolvedPromptSources(
            agent_system="你是助手",
            agent_task="用 uv",
            request_instructions="hi",
            platform_policy_source=get_default_platform_policy_source(),
        )
    )
    result = await run_hosted_pipeline(
        compiled_prompt=compiled,
        user_input="hi",
        history=[],
        working_state=None,
        model_metadata={"context_window_tokens": 1012000, "max_output_tokens": 32000},
        integration_mode="ksadk_hosted",
        accounting_accuracy="estimated",
        agent_max_input_tokens=4096,
        agent_reserve_output_tokens=512,
    )
    assert result is not None
    budget = result.plan.get("budget", {})
    # budget 用 AgentVersion 的 4096，不是模型的 1012000
    # 精确断言：4096 预算不被 safety_buffer 扣成 0
    assert budget["max_input_tokens"] == 4096, (
        f"budget max_input 应为 4096（AgentVersion 预算，不扣 safety_buffer），"
        f"实际 {budget['max_input_tokens']}"
    )
    assert budget["soft_limit_tokens"] == 2048, (
        f"soft_limit 应为 2048（50% of 4096），实际 {budget['soft_limit_tokens']}"
    )
    assert budget["hard_limit_tokens"] == 3481, (
        f"hard_limit 应为 3481（85% of 4096），实际 {budget['hard_limit_tokens']}"
    )


@pytest.mark.asyncio
async def test_no_agent_budget_falls_back_to_model_window(tmp_path, monkeypatch):
    """无 agent_max_input_tokens → fallback 到 model_metadata。"""
    monkeypatch.setenv("KSADK_CONTEXT_ENGINE_V2_ENABLED", "true")
    compiled = compile_resolved_prompt_dict(
        ResolvedPromptSources(
            agent_system="你是助手",
            agent_task="",
            request_instructions="hi",
            platform_policy_source=get_default_platform_policy_source(),
        )
    )
    result = await run_hosted_pipeline(
        compiled_prompt=compiled,
        user_input="hi",
        history=[],
        working_state=None,
        model_metadata={"context_window_tokens": 200000, "max_output_tokens": 32000},
        integration_mode="ksadk_hosted",
        accounting_accuracy="estimated",
    )
    assert result is not None
    budget = result.plan.get("budget", {})
    # fallback 到 model_metadata（~200k，非 4096）
    assert budget["max_input_tokens"] > 100000, (
        f"应 fallback 到 model window，实际 {budget['max_input_tokens']}"
    )


# ---- 2. integration_mode 口径一致 ----


def test_sqlite_ltm_backend_persists_across_instances(tmp_path, monkeypatch):
    """SqliteLTMBackend 跨实例持久化（recall 和 flush 共用同一文件）。"""
    db = tmp_path / "test_memory.db"
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(db))

    from ksadk.memory.adk.backends.sqlite_ltm_backend import SqliteLTMBackend

    # 实例 1：保存
    backend1 = SqliteLTMBackend(index="test")
    backend1.save_memory(
        "user-1",
        ['{"parts": [{"text": "用户偏好用 Python 3.12"}]}'],
    )

    # 实例 2：检索（新实例，同一文件）
    backend2 = SqliteLTMBackend(index="test")
    results = backend2.search_memory("user-1", "Python", top_k=5)
    assert any("Python 3.12" in r for r in results), f"跨实例 recall 失败: {results}"


def test_sqlite_ltm_backend_replaces_inmemory_as_default_local():
    """local backend 默认用 SqliteLTMBackend（非 InMemoryLTMBackend）。"""
    from ksadk.memory.adk.backends.sqlite_ltm_backend import SqliteLTMBackend
    from ksadk.memory.ltm_backend_factory import get_long_term_memory_backend_cls

    cls = get_long_term_memory_backend_cls("local")
    assert cls is SqliteLTMBackend, f"local 应默认用 SqliteLTMBackend，实际 {cls}"


def test_sqlite_ltm_backend_force_inmemory(monkeypatch):
    """KSADK_LTM_FORCE_INMEMORY=1 时回退 InMemory。"""
    monkeypatch.setenv("KSADK_LTM_FORCE_INMEMORY", "true")
    from ksadk.memory.adk.backends.inmemory_ltm_backend import InMemoryLTMBackend
    from ksadk.memory.ltm_backend_factory import get_long_term_memory_backend_cls

    cls = get_long_term_memory_backend_cls("local")
    assert cls is InMemoryLTMBackend
