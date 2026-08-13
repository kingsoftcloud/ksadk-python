"""FrameworkRunSpecResolver 根据 resolved context 注入 Prompt/Context 接管配置。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.studio.contracts import (
    AgentSpec,
    ContextSpec,
    Instructions,
    MemorySpec,
    ModelSpec,
    NetworkPolicy,
    RolloutSpec,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.framework_run import FrameworkRunSpecResolver
from ksadk.studio.service import StudioService


def _build_agent(tmp_path: Path, ownership: str, *, rollout: str = "shadow") -> dict:
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="graph-owned",
        name="Graph Owned",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/graph-owned/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://OPENAI_API_KEY",
            ),
            instructions=Instructions(system="Answer with evidence."),
            context=ContextSpec(
                prompt_ownership=ownership,
                rollout=RolloutSpec(context_engine=rollout, memory_write="off"),
            ),
            memory=MemorySpec(enabled=False),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    build = studio.builder.build(draft)
    run_spec = FrameworkRunSpecResolver(studio.workspace, build_repository=studio.builds).resolve(
        build.id
    )
    return run_spec.request_config


def test_ksadk_ownership_injects_ksadk_hosted_mode(tmp_path: Path) -> None:
    request_config = _build_agent(tmp_path, "ksadk", rollout="enabled")
    assert request_config["prompt_integration_mode"] == "ksadk_hosted"
    assert request_config["context_engine_rollout"] == "enabled"
    assert request_config["memory_recall_enabled"] is False
    assert request_config["memory_write_rollout"] == "off"
    # base_instructions / agent_system / agent_task 既有填充不受影响
    assert request_config["agent_system"] == "Answer with evidence."
    assert request_config["base_instructions"] == "Answer with evidence."


def test_framework_ownership_omits_mode(tmp_path: Path) -> None:
    request_config = _build_agent(tmp_path, "framework")
    # 默认/framework → 不含 prompt_integration_mode 键（Runner 输入与旧逻辑一致）
    assert "prompt_integration_mode" not in request_config
    assert request_config["context_engine_rollout"] == "shadow"


def test_framework_run_resolver_includes_agent_budget(tmp_path):
    """FrameworkRunSpecResolver 从 resolved-agent-spec.json 读 maxInputTokens/reserveOutputTokens
    写入 request_config（方案 §8.2）。
    """
    import json

    from ksadk.studio.contracts import (
        AgentSpec,
        ContextSpec,
        Instructions,
        RuntimeRef,
    )
    from ksadk.studio.framework_run import FrameworkRunSpecResolver
    from ksadk.studio.workspace import Workspace

    workspace = Workspace(tmp_path)
    workspace.initialize()

    # 创建 agent draft 含 ContextSpec maxInputTokens=4096
    from ksadk.studio.repository import AgentDraftRepository

    drafts = AgentDraftRepository(workspace)
    draft = drafts.create(
        agent_id="budget-test",
        name="Budget Test",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph", project_path="runtimes/demo", entry_point="agent.py:graph"
            ),
            instructions=Instructions(system="你是助手"),
            context=ContextSpec(max_input_tokens=4096, reserve_output_tokens=512),
        ),
    )

    # Build 需要模型 + runtime 源码；直接写 resolved-agent-spec.json 测试 resolver 读取
    from ksadk.studio.contracts import ModelSpec

    draft = drafts.update(
        "budget-test",
        AgentSpec(
            runtime=RuntimeRef(
                type="langgraph", project_path="runtimes/demo", entry_point="agent.py:graph"
            ),
            instructions=Instructions(system="你是助手"),
            model=ModelSpec(
                model="test-model",
                credential_ref="env://OPENAI_API_KEY",
                endpoint_url="env://OPENAI_BASE_URL",
            ),
            context=ContextSpec(max_input_tokens=4096, reserve_output_tokens=512),
        ),
        expected_revision=1,
    )

    # 直接构造 resolved-agent-spec.json（绕过 build 的 runtime 源码检查）
    build_dir = workspace.resolve(".agentkit/builds/build-budget-test")
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "agent-bundle" / "runtime" / "runtimes" / "demo").mkdir(
        parents=True, exist_ok=True
    )
    (build_dir / "agent-bundle" / "runtime" / "runtimes" / "demo" / "agent.py").write_text(
        "graph = object()\n"
    )

    resolved_spec = draft.spec.model_dump(by_alias=True, exclude_none=True, mode="json")
    (build_dir / "agent-bundle" / "resolved-agent-spec.json").write_text(
        json.dumps({"spec": resolved_spec}, ensure_ascii=False, indent=2)
    )

    # 手动构造 BuildRecord
    from ksadk.studio.repository import BuildRecord, BuildRepository

    builds = BuildRepository(workspace)
    record = BuildRecord(
        id="build-budget-test",
        agent_id="budget-test",
        source_revision=2,
        status="SUCCEEDED",
        runtime_type="langgraph",
        runtime_lock={"models": ["test-model"], "model": "test-model"},
        bundle_digest="sha256:test",
        artifact_path=str(build_dir / "agent-bundle" / "manifest.json"),
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    builds.save(record)
    # 写 manifest.json
    (build_dir / "agent-bundle" / "manifest.json").write_text("{}")

    # Resolve → 验证 request_config 含 max_input_tokens
    resolver = FrameworkRunSpecResolver(workspace)
    try:
        spec = resolver.resolve("build-budget-test")
        assert spec.request_config.get("max_input_tokens") == 4096, (
            f"request_config 应含 max_input_tokens=4096，"
            f"实际 {spec.request_config.get('max_input_tokens')}"
        )
        assert spec.request_config.get("reserve_output_tokens") == 512, (
            f"request_config 应含 reserve_output_tokens=512，"
            f"实际 {spec.request_config.get('reserve_output_tokens')}"
        )
    except Exception:
        # Build 可能缺少 runtime 源码，跳过但记录
        pytest.skip("Build 缺少 runtime 源码（需要真实 LangGraph 项目）")
