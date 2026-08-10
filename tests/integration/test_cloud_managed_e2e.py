"""云端预发 E2E 占位（方案 §17.5 / §6.4）。

云端预发 E2E 需要 ``ksadk_managed_cloud`` 部署：agentengine-server 注册 AgentVersion、
真实 Runtime template id、云端 Memory Service endpoint 与凭证、远端 Session Store。本机无法
提供这些条件，故本测试以 ``pytest.skip`` 占位并明确列出缺失条件——不假装能跑（方案 §7
"不能用应该能联通代替结果"）。

满足条件后应替换为真实调用：经 RuntimeExecutor 启动 ``ksadk_managed_cloud`` 部署的 Agent，
断言同一 AgentVersion 在 local 与 ksadk_managed_cloud 生成相同 canonical CompiledPrompt hash
（方案 §12）、deployment_mode 与 integration_mode 同时写入 Run/Trace（ADR-018）、
Managed Codex 路径 ownership 仍为 native_runtime（PCM-DEPLOY-002）。
"""

from __future__ import annotations

import os

import pytest

_SKIP_REASONS: list[str] = []


def _missing() -> list[str]:
    reasons: list[str] = []
    if not os.environ.get("KSADK_CLOUD_AGENTENGINE_ENDPOINT"):
        reasons.append("缺少 agentengine-server endpoint（KSADK_CLOUD_AGENTENGINE_ENDPOINT）")
    if not os.environ.get("KSADK_CLOUD_AGENT_ID"):
        reasons.append("缺少已注册的 AgentVersion（KSADK_CLOUD_AGENT_ID）")
    if not os.environ.get("KSADK_CLOUD_RUNTIME_TEMPLATE_ID"):
        reasons.append("缺少 Runtime template id（KSADK_CLOUD_RUNTIME_TEMPLATE_ID）")
    if not os.environ.get("KSADK_CLOUD_MEMORY_ENDPOINT"):
        reasons.append("缺少云端 Memory Service endpoint（KSADK_CLOUD_MEMORY_ENDPOINT）")
    if not os.environ.get("KSADK_CLOUD_DEPLOYMENT_MODE"):
        reasons.append("缺少 deployment_mode 声明（KSADK_CLOUD_DEPLOYMENT_MODE）")
    return reasons


@pytest.mark.skipif(
    bool(_missing()),
    reason="云端预发 E2E 缺少真实部署条件：" + "; ".join(_missing()),
)
@pytest.mark.asyncio
async def test_cloud_managed_e2e_canonical_consistency():
    """local 与 ksadk_managed_cloud 同一 AgentVersion 生成相同 canonical CompiledPrompt hash（PCM-DEPLOY-001）。"""
    # 满足条件后实现：本地 build_run_input 编译 hash == 云端 Runtime 启动后 trace 中的 prompt_content_hash
    pytest.fail("cloud e2e 未实现：条件满足后替换为真实 RuntimeExecutor 启动 + hash 比对")


@pytest.mark.skipif(
    bool(_missing()),
    reason="云端预发 E2E 缺少真实部署条件：" + "; ".join(_missing()),
)
@pytest.mark.asyncio
async def test_cloud_managed_deployment_writes_two_axes():
    """Run/Trace 同时记录 deployment_mode + integration_mode（ADR-018 / PCM-DEPLOY-002）。"""
    pytest.fail("cloud e2e 未实现：条件满足后断言 trace 同时含 context.deployment_mode 与 context.integration_mode")


# ---- 本机可验证的云端一致性合同（PCM-DEPLOY-001 的本地侧）----
# 真实跨部署云端 E2E 需要预发部署（缺凭证，见上 skip）。这里验证一致性合同的本地侧：
# 同一 AgentVersion 在 build 时与 run 时编译出相同 canonical hash，且 deployment_mode
# 独立写入不改变 ownership——这是本地与云端能对齐的前置条件（方案 §12）。


@pytest.mark.asyncio
async def test_local_side_canonical_hash_is_deterministic_across_runs(monkeypatch):
    """同一 AgentVersion 两次 build_run_input 产出相同 prompt_content_hash（确定性，方案 §12）。"""
    from ksadk.conversations.runtime_preparation import build_run_input
    from ksadk.sessions.in_memory import InMemorySessionService

    monkeypatch.setenv("KSADK_CONTEXT_ENGINE_V2_ENABLED", "true")
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "true")

    async def _build():
        service = InMemorySessionService()
        return await build_run_input(
            agent_id="a", user_id="u", session_id="s",
            messages=[{"role": "user", "content": "x"}], model="m",
            instructions="q", agent_system="你是助手", agent_task="用 uv",
            prompt_integration_mode="ksadk_hosted", runtime_type="langgraph",
            session_service_provider=lambda: service,
        )

    p1 = await _build()
    p2 = await _build()
    h1 = (p1.compiled_prompt or {}).get("prompt_content_hash")
    h2 = (p2.compiled_prompt or {}).get("prompt_content_hash")
    assert h1 and h2 and h1 == h2, "同一 AgentVersion 的 canonical prompt hash 必须确定且一致"


@pytest.mark.asyncio
async def test_deployment_mode_change_does_not_silently_change_ownership(monkeypatch):
    """PCM-DEPLOY-002：deployment_mode 变化不改 integration_mode（ownership 正交）。"""
    from ksadk.conversations.runtime_preparation import build_run_input
    from ksadk.sessions.in_memory import InMemorySessionService

    monkeypatch.setenv("KSADK_CONTEXT_ENGINE_V2_ENABLED", "true")
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "true")

    async def _build(deployment_mode):
        service = InMemorySessionService()
        return await build_run_input(
            agent_id="a", user_id="u", session_id="s",
            messages=[{"role": "user", "content": "x"}], model="m",
            instructions="q", agent_system="你是助手", agent_task="",
            prompt_integration_mode="ksadk_hosted", runtime_type="langgraph",
            deployment_mode=deployment_mode,
            session_service_provider=lambda: service,
        )

    local = await _build("local")
    cloud = await _build("ksadk_managed_cloud")
    # ownership 不随 deployment 变化（langgraph 均为 framework_assisted，prompt_owner=ksadk）
    assert local.shadow_context_plan["integration_mode"] == cloud.shadow_context_plan["integration_mode"]
    assert local.shadow_context_plan["prompt_owner"] == cloud.shadow_context_plan["prompt_owner"]
    # deployment_mode 确实独立写入且不同
    assert local.shadow_context_plan["deployment_mode"] == "local"
    assert cloud.shadow_context_plan["deployment_mode"] == "ksadk_managed_cloud"
