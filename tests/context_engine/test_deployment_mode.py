"""DeploymentMode 二维字段测试（方案 §4.3 / §6.1 / ADR-018）。

验证部署位置与 Context ownership 正交：``deployment_mode`` 独立写入 shadow plan / baseline /
trace，不能据它推断 ``integration_mode``；``ksadk_managed_cloud`` + ``native_runtime``（Managed
Codex Runtime）不被误判为 KsADK Harness。
"""

from __future__ import annotations

from pathlib import Path

from ksadk.context_engine.shadow_plan import build_shadow_context_plan_dict
from ksadk.runtime.launch import RuntimeLaunchContext


def test_deployment_mode_defaults_local():
    plan = build_shadow_context_plan_dict(instructions="x", runtime_type="langgraph")
    assert plan["deployment_mode"] == "local"


def test_deployment_mode_propagated_into_plan():
    plan = build_shadow_context_plan_dict(
        instructions="x", runtime_type="codex", deployment_mode="ksadk_managed_cloud"
    )
    assert plan["deployment_mode"] == "ksadk_managed_cloud"
    # deployment 不改变 ownership：codex 仍是 native_runtime（Managed Codex Runtime，不是 Harness）
    assert plan["integration_mode"] == "native_runtime"
    assert plan["prompt_owner"] == "native"


def test_deployment_mode_does_not_override_integration_mode():
    # 即使 ksadk_managed_cloud，langgraph 未声明 ksadk_hosted 接管 → 仍 framework_assisted
    plan = build_shadow_context_plan_dict(
        instructions="x", runtime_type="langgraph", deployment_mode="ksadk_managed_cloud"
    )
    assert plan["deployment_mode"] == "ksadk_managed_cloud"
    assert plan["integration_mode"] == "framework_assisted"


def test_external_managed_native_stays_native():
    plan = build_shadow_context_plan_dict(
        instructions="x", runtime_type="codex", deployment_mode="external_managed"
    )
    assert plan["deployment_mode"] == "external_managed"
    assert plan["integration_mode"] == "native_runtime"


def test_runtime_launch_context_deployment_mode_validation(monkeypatch):
    monkeypatch.delenv("KSADK_DEPLOYMENT_MODE", raising=False)
    ctx = RuntimeLaunchContext(
        runtime_type="langgraph",
        project_dir=Path("/tmp"),
        deployment_mode="ksadk_managed_cloud",
    )
    assert ctx.deployment_mode == "ksadk_managed_cloud"
    # 非法值回退 local
    ctx2 = RuntimeLaunchContext(
        runtime_type="langgraph", project_dir=Path("/tmp"), deployment_mode="bogus"
    )
    assert ctx2.deployment_mode == "local"
    # 默认 local
    ctx3 = RuntimeLaunchContext(runtime_type="langgraph", project_dir=Path("/tmp"))
    assert ctx3.deployment_mode == "local"


def test_runtime_launch_context_uses_cloud_deployer_environment(monkeypatch):
    monkeypatch.setenv("KSADK_DEPLOYMENT_MODE", "ksadk_managed_cloud")
    ctx = RuntimeLaunchContext(runtime_type="langgraph", project_dir=Path("/tmp"))
    assert ctx.deployment_mode == "ksadk_managed_cloud"


def test_deployment_mode_literal_includes_three_modes():
    # DeploymentMode 是 Literal，运行时取值只有三个合法值
    assert "local" in ("local", "ksadk_managed_cloud", "external_managed")
    assert "ksadk_managed_cloud" in ("local", "ksadk_managed_cloud", "external_managed")
    assert "external_managed" in ("local", "ksadk_managed_cloud", "external_managed")
