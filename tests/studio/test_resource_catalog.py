from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from ksadk.studio.compiler import AgentCompiler
from ksadk.studio.contracts import (
    AgentBindings,
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    CapabilityBinding,
    Instructions,
    MCPServerRef,
    ModelParameters,
    ModelSpec,
    ToolContract,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.workspace import Workspace


def _catalog(tmp_path: Path) -> LocalResourceCatalog:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    return LocalResourceCatalog(workspace)


def _skill_zip(*, entry: str = "research/SKILL.md") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            entry,
            "---\n"
            "name: Research Skill\n"
            "description: Search primary sources\n"
            "version: 1.2.0\n"
            "---\n"
            "Always cite primary sources.\n",
        )
        archive.writestr("research/references/checklist.md", "Check dates.\n")
    return stream.getvalue()


def test_catalog_lists_stable_builtin_model_and_tool_resources(tmp_path: Path):
    catalog = _catalog(tmp_path)

    first = catalog.list()
    second = catalog.list()

    assert [item.resource_id for item in first] == [
        item.resource_id for item in second
    ]
    assert any(
        item.resource_id == "model:builtin:glm-5-1:1.0.0" for item in first
    )
    builtin_tools = {
        item.name: item for item in first if item.kind == "tool" and item.source == "builtin"
    }
    assert "read_workspace_file" in builtin_tools
    assert "tool_search" in builtin_tools
    assert builtin_tools["read_workspace_file"].category == "workspace"
    assert builtin_tools["read_workspace_file"].contract["boundary"] == "workspace_root"
    assert "builtin.echo" not in builtin_tools


@pytest.mark.asyncio
async def test_provider_models_reuse_ksadk_metadata_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _provider_catalog(**_kwargs):
        return [
            {
                "id": "vision-model",
                "display_name": "Vision Model",
                "context_window_tokens": 131072,
                "max_output_tokens": 8192,
                "architecture": {
                    "input_modalities": ["文字", "图片"],
                    "output_modalities": ["文字"],
                },
                "capabilities": {
                    "multimodal_input_image": True,
                    "multimodal_input_video": False,
                    "multimodal_input_file": False,
                },
                "limits": {
                    "context_window_tokens": 131072,
                    "max_output_tokens": 8192,
                },
                "_provider_raw_model": {
                    "id": "vision-model",
                    "display_name": "Vision Model",
                    "context_length": 131072,
                    "architecture": {"input_modalities": ["文字", "图片"]},
                },
            }
        ]

    monkeypatch.setattr(
        "ksadk.studio.resource_catalog.fetch_provider_model_catalog",
        _provider_catalog,
    )
    catalog = _catalog(tmp_path)
    actual, actual_source = await catalog.discover_provider_models(
        api_base="https://models.example.test/v1",
        api_key="secret",
        current_model="vision-model",
    )

    assert actual_source == "provider"
    assert [item.name for item in actual] == ["vision-model"]
    descriptor = actual[0]
    assert descriptor.source == "provider"
    assert descriptor.contract["metadata"]["context_window_tokens"] == 131072
    assert descriptor.contract["metadata"]["capabilities"]["multimodal_input_image"] is True
    assert descriptor.contract["discovery"]["contextWindow"] == "provider"
    assert descriptor.contract["discovery"]["inputModalities"] == "provider"


def test_catalog_persists_model_mcp_and_custom_tool_resources(tmp_path: Path):
    catalog = _catalog(tmp_path)
    model = catalog.create_model_profile(
        name="private-model",
        display_name="Private Model",
        version="2.1.0",
        description="Private gateway",
        spec=ModelSpec(
            model="private-model",
            endpoint_url="https://model.example.com/v1/chat/completions",
            credential_ref="env://PRIVATE_MODEL_KEY",
        ),
    )
    mcp = catalog.create_mcp_server(
        display_name="Filesystem MCP",
        description="Local MCP",
        server=MCPServerRef(
            name="filesystem",
            version="1.0.0",
            transport="stdio",
            command="filesystem-mcp",
            env_refs={"TOKEN": "env://FILESYSTEM_TOKEN"},
        ),
    )
    tool = catalog.create_tool(
        display_name="Ask Operator",
        category="interaction",
        contract=ToolContract(
            name="ask_operator",
            version="1.0.0",
            description="Ask the session operator",
            executor="deferred",
            input_schema={
                "type": "object",
                "required": ["question"],
                "properties": {"question": {"type": "string"}},
            },
        ),
    )

    reloaded = LocalResourceCatalog(catalog.workspace)
    assert reloaded.get(model.resource_id).contract["model"] == "private-model"
    assert reloaded.get(mcp.resource_id).required_secret_refs == [
        "env://FILESYSTEM_TOKEN"
    ]
    assert reloaded.get(tool.resource_id).contract["executor"] == "deferred"


def test_policy_preview_maps_strict_loose_and_custom_approvals(tmp_path: Path):
    catalog = _catalog(tmp_path)
    tools = {item.name: item for item in catalog.list(kind="tool", limit=100)}
    read = CapabilityBinding(resource_id=tools["read_workspace_file"].resource_id)
    write = CapabilityBinding(resource_id=tools["write_workspace_file"].resource_id)

    strict, permissions = catalog.policy_preview(
        AgentBindings(policy_template="strict", tools=[read, write])
    )
    loose, _ = catalog.policy_preview(
        AgentBindings(policy_template="loose", tools=[read, write])
    )
    custom, _ = catalog.policy_preview(
        AgentBindings(
            policy_template="custom",
            tools=[
                read,
                write.model_copy(update={"approval": "never"}),
            ],
        )
    )

    assert {tool.name: tool.approval for tool in strict} == {
        "read_workspace_file": "never",
        "write_workspace_file": "always",
    }
    assert {tool.approval for tool in loose} == {"never"}
    assert {tool.name: tool.approval for tool in custom}["write_workspace_file"] == (
        "never"
    )
    assert permissions == ["workspace:file:read", "workspace:file:write"]


def test_compiler_materializes_bindings_into_immutable_dependencies(tmp_path: Path):
    catalog = _catalog(tmp_path)
    model = catalog.list(kind="model")[0]
    tools = {item.name: item for item in catalog.list(kind="tool", limit=100)}
    draft = AgentDraft(
        metadata=AgentMetadata(id="bound-agent", name="Bound Agent"),
        spec=AgentSpec(
            instructions=Instructions(system="Use only selected tools."),
            bindings=AgentBindings(
                model_profile_id=model.resource_id,
                model_parameters=ModelParameters(max_tokens=777),
                policy_template="strict",
                tools=[
                    CapabilityBinding(
                        resource_id=tools["read_workspace_file"].resource_id
                    ),
                    CapabilityBinding(
                        resource_id=tools["write_workspace_file"].resource_id
                    ),
                ],
            ),
        ),
    )

    result = AgentCompiler(catalog.workspace, catalog=catalog).compile(draft)

    assert result.resolved.model.model == "glm-5.1"
    assert result.resolved.model.parameters.max_tokens == 777
    assert {tool.name for tool in result.resolved.capabilities.tools} == {
        "read_workspace_file",
        "write_workspace_file",
    }
    assert {
        tool.name: tool.approval for tool in result.resolved.capabilities.tools
    }["write_workspace_file"] == "always"
    assert "workspace:file:write" in result.resolved.security.allowed_permissions
    assert result.dependency_lock["model"]["model"] == "glm-5.1"


def test_skill_zip_import_is_installed_and_content_addressed(tmp_path: Path):
    catalog = _catalog(tmp_path)

    descriptor = catalog.import_skill_zip(
        _skill_zip(),
        filename="research.zip",
    )

    assert descriptor.kind == "skill"
    assert descriptor.status == "ready"
    assert descriptor.version == "1.2.0"
    assert descriptor.digest.startswith("sha256:")
    installed = tmp_path / "capabilities/skills/research-skill"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "references/checklist.md").is_file()


def test_skill_zip_import_rejects_directory_traversal(tmp_path: Path):
    catalog = _catalog(tmp_path)

    with pytest.raises(StudioError) as captured:
        catalog.import_skill_zip(
            _skill_zip(entry="../SKILL.md"),
            filename="unsafe.zip",
        )

    assert captured.value.code == "SKILL_ARCHIVE_UNSAFE"
    assert not (tmp_path.parent / "SKILL.md").exists()
