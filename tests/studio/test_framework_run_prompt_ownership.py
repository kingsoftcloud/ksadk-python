"""FrameworkRunSpecResolver 根据 resolved context 注入 Prompt/Context 接管配置。"""

from __future__ import annotations

from pathlib import Path

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
    run_spec = FrameworkRunSpecResolver(
        studio.workspace, build_repository=studio.builds
    ).resolve(build.id)
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
