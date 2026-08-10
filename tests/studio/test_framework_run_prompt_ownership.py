"""PR B：FrameworkRunSpecResolver 据 resolved spec 的 context.prompt_ownership 注入 prompt_integration_mode。"""

from __future__ import annotations

import json
from pathlib import Path

from ksadk.studio.contracts import (
    AgentSpec,
    ContextSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.framework_run import FrameworkRunSpecResolver
from ksadk.studio.service import StudioService


def _build_agent(tmp_path: Path, ownership: str) -> str:
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
            context=ContextSpec(prompt_ownership=ownership),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    build = studio.builder.build(draft)
    run_spec = FrameworkRunSpecResolver(
        studio.workspace, build_repository=studio.builds
    ).resolve(build.id)
    return run_spec.request_config


def test_ksadk_ownership_injects_ksadk_hosted_mode(tmp_path: Path) -> None:
    request_config = _build_agent(tmp_path, "ksadk")
    assert request_config["prompt_integration_mode"] == "ksadk_hosted"
    # base_instructions / agent_system / agent_task 既有填充不受影响
    assert request_config["agent_system"] == "Answer with evidence."
    assert request_config["base_instructions"] == "Answer with evidence."


def test_framework_ownership_omits_mode(tmp_path: Path) -> None:
    request_config = _build_agent(tmp_path, "framework")
    # 默认/framework → 不含 prompt_integration_mode 键（Runner 输入与旧逻辑一致）
    assert "prompt_integration_mode" not in request_config
