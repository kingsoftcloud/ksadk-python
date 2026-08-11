from __future__ import annotations

from pathlib import Path

from ksadk.studio.contracts import (
    AgentTemplateComposeRequest,
    MCPServerRef,
    ModelSpec,
)
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.templates import (
    RESEARCH_SKILL_NAME,
    compose_blank_agent,
    compose_research_agent,
)
from ksadk.studio.workspace import Workspace


def _register_model(catalog: LocalResourceCatalog) -> None:
    catalog.create_model_profile(
        name="glm-5.1",
        display_name="GLM-5.1",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            provider="openai-compatible",
            model="glm-5.1",
            endpoint_url="https://api.openai.com/v1/chat/completions",
            credential_ref="env://AGENTKIT_MODEL_API_KEY",
        ),
    )


def test_blank_template_preserves_prompt_and_explicit_capabilities(tmp_path: Path):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    catalog = LocalResourceCatalog(workspace)
    _register_model(catalog)
    read_tool = next(
        item for item in catalog.list(limit=200) if item.name == "read_workspace_file"
    )
    prompt = "你是一名企业技术支持助手。只根据已知信息回答，缺少关键上下文时先提问，不要虚构事实。"

    composition = compose_blank_agent(
        workspace,
        catalog,
        AgentTemplateComposeRequest(
            prompt=prompt,
            description="处理企业技术支持问题",
            tool_resource_ids=[read_tool.resource_id],
            policy_template="loose",
            execution_strategy="direct",
            max_steps=6,
            timeout_seconds=90,
        ),
    )

    assert composition.template_id == "blank"
    assert composition.spec.description == "处理企业技术支持问题"
    assert composition.spec.instructions.system == prompt
    assert composition.spec.instructions.task
    assert composition.spec.execution.strategy == "direct"
    assert composition.spec.execution.max_steps == 6
    assert composition.spec.execution.timeout_seconds == 90
    assert composition.spec.bindings.policy_template == "loose"
    assert [item.resource_id for item in composition.spec.bindings.tools] == [read_tool.resource_id]
    assert composition.spec.bindings.skills == []
    assert composition.spec.bindings.mcp_servers == []
    assert composition.warnings == []
    assert not (tmp_path / "capabilities/skills" / RESEARCH_SKILL_NAME).exists()


def test_research_template_installs_and_binds_methodology_skill(tmp_path: Path):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    catalog = LocalResourceCatalog(workspace)
    _register_model(catalog)

    composition = compose_research_agent(
        workspace,
        catalog,
        AgentTemplateComposeRequest(
            goal="调研企业采用端云一体 Agent 平台的技术路径和主要风险",
            audience="平台架构委员会",
            depth="deep",
        ),
    )

    assert composition.template_id == "research"
    assert composition.spec.execution.strategy == "plan-act-observe"
    assert composition.spec.execution.max_steps == 28
    assert composition.spec.bindings.model_profile_id
    assert "端云一体 Agent 平台" in composition.spec.instructions.system
    assert "当前未绑定外部调研 MCP" in composition.spec.instructions.system
    assert composition.warnings
    assert any(
        item.kind == "mcp" and item.status == "missing" for item in composition.recommendations
    )
    assert (tmp_path / "capabilities/skills" / RESEARCH_SKILL_NAME / "SKILL.md").is_file()
    skill = catalog.get(composition.spec.bindings.skills[0].resource_id)
    assert skill.name == RESEARCH_SKILL_NAME
    assert skill.version == "1.0.0"


def test_research_template_auto_binds_compatible_mcp(tmp_path: Path):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    catalog = LocalResourceCatalog(workspace)
    _register_model(catalog)
    server = MCPServerRef(
        name="web-research",
        version="1.0.0",
        transport="stdio",
        command="web-research",
    )
    descriptor = catalog.create_mcp_server(
        display_name="Web Research MCP",
        description="Search and fetch public web sources",
        server=server,
    )
    catalog.save_probe(
        descriptor.resource_id,
        result={
            "serverInfo": {"name": "web-research", "version": "1.0.0"},
            "tools": [
                {
                    "name": "search_web",
                    "version": "1.0.0",
                    "description": "Search public web sources",
                    "executor": "mcp",
                    "mcpServer": "web-research",
                }
            ],
        },
    )

    composition = compose_research_agent(
        workspace,
        catalog,
        AgentTemplateComposeRequest(
            goal="调研 Agent 工程平台的主流能力边界和竞争格局",
        ),
    )

    assert [item.resource_id for item in composition.spec.bindings.mcp_servers] == [
        descriptor.resource_id
    ]
    assert composition.warnings == []
    assert "当前未绑定外部调研 MCP" not in composition.spec.instructions.system
    assert any(
        item.kind == "mcp" and item.status == "bound" and item.resource_id == descriptor.resource_id
        for item in composition.recommendations
    )


def test_research_template_does_not_bind_unprobed_browser_mcp(tmp_path: Path):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    catalog = LocalResourceCatalog(workspace)
    _register_model(catalog)
    catalog.create_mcp_server(
        display_name="Browser MCP",
        description="Browser research connector without a completed probe",
        server=MCPServerRef(
            name="browser-mcp",
            version="1.0.0",
            transport="stdio",
            command="browser-mcp",
        ),
    )

    composition = compose_research_agent(
        workspace,
        catalog,
        AgentTemplateComposeRequest(
            goal="调研 Agent 平台的外部信息检索与引用能力",
        ),
    )

    assert composition.spec.bindings.mcp_servers == []
    assert composition.warnings
    assert any(
        item.kind == "mcp" and item.status == "missing" for item in composition.recommendations
    )
