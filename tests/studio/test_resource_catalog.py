from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
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
    RuntimeRef,
    ToolContract,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.python_tool_inspection import PythonToolInspector
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.service import StudioService
from ksadk.studio.workspace import Workspace


def _catalog(tmp_path: Path) -> LocalResourceCatalog:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    return LocalResourceCatalog(workspace)


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


def test_python_tool_inspection_uses_ast_without_executing_module(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    inspector = PythonToolInspector(workspace)
    marker = tmp_path / "executed.txt"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "def safe(value: str, limit: int = 3) -> str:\n"
        '    """Return a safe value."""\n'
        "    return value[:limit]\n"
        "async def async_safe(query: str) -> dict:\n"
        "    return {'query': query}\n"
    ).encode()

    result = inspector.inspect(source, filename="tool.py")

    assert [item["name"] for item in result["callables"]] == ["safe", "async_safe"]
    assert result["callables"][0]["required"] == ["value"]
    assert not marker.exists()


@pytest.mark.parametrize(
    ("content", "filename", "code"),
    [
        (b"\xff", "tool.py", "PYTHON_TOOL_ENCODING_INVALID"),
        (b"def ok():\n    pass\n", "tool.txt", "PYTHON_TOOL_FILENAME_INVALID"),
        (b"def broken(:\n", "tool.py", "PYTHON_TOOL_SYNTAX_INVALID"),
    ],
)
def test_python_tool_inspection_rejects_invalid_sources(
    tmp_path: Path,
    content: bytes,
    filename: str,
    code: str,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    inspector = PythonToolInspector(workspace)
    with pytest.raises(StudioError) as raised:
        inspector.inspect(content, filename=filename)
    assert raised.value.code == code


def test_python_tool_inspection_rejects_digest_change_before_commit(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    inspector = PythonToolInspector(workspace)
    inspected = inspector.inspect(b"def safe(value):\n    return value\n", filename="tool.py")
    staged = inspector.root / inspected["inspectionToken"] / "tool.py"
    staged.write_text("def changed():\n    return 1\n")

    with pytest.raises(StudioError) as raised:
        inspector.load(inspected["inspectionToken"])
    assert raised.value.code == "PYTHON_TOOL_INSPECTION_CHANGED"


def test_python_tool_commit_snapshots_only_the_inspected_callable(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    inspected = catalog.inspect_python_tool(
        b"def search(query, limit=5):\n    return [query][:limit]\n",
        filename="search_tool.py",
    )

    descriptor = catalog.commit_python_tool(
        inspected["inspectionToken"],
        display_name="Search Tool",
        name="search_tool",
        callable_name="search",
        description="Search safely",
    )

    assert descriptor.contract["callableName"] == "search"
    assert descriptor.contract["sourceSha256"].startswith("sha256:")
    assert descriptor.contract["inputSchema"]["required"] == ["query"]
    assert not (catalog.python_tool_inspector.root / inspected["inspectionToken"]).exists()


def test_python_tool_http_flow_requires_inspect_then_commit(tmp_path: Path) -> None:
    service = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/catalog/python-tools:inspect",
            files={
                "file": (
                    "math_tool.py",
                    b"def add(left, right):\n    return left + right\n",
                    "text/x-python",
                )
            },
        )
        assert inspected.status_code == 200
        token = inspected.json()["inspectionToken"]
        created = client.post(
            f"/api/v1/catalog/python-tools/{token}:commit",
            json={
                "displayName": "Add",
                "name": "add_numbers",
                "callableName": "add",
                "description": "Add values",
            },
        )
        assert created.status_code == 201
        assert created.json()["contract"]["callableName"] == "add"


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


def test_catalog_lists_stable_builtin_tool_resources(tmp_path: Path):
    catalog = _catalog(tmp_path)

    first = catalog.list()
    second = catalog.list()

    assert [item.resource_id for item in first] == [item.resource_id for item in second]
    builtin_tools = {
        item.name: item for item in first if item.kind == "tool" and item.source == "builtin"
    }
    assert "read_workspace_file" in builtin_tools
    assert "tool_search" in builtin_tools
    assert builtin_tools["read_workspace_file"].category == "workspace"
    assert builtin_tools["read_workspace_file"].contract["boundary"] == "workspace_root"
    assert "builtin.echo" not in builtin_tools


def test_catalog_cursor_is_stable_and_uses_resource_id_as_tie_breaker(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    for name in ("model-a", "model-b", "model-c"):
        catalog.create_model_profile(
            name=name,
            display_name="Same display name",
            version="1.0.0",
            description="cursor fixture",
            spec=ModelSpec(
                model=name,
                endpoint_url="https://api.openai.com/v1/chat/completions",
                credential_ref="env://AGENTKIT_MODEL_API_KEY",
            ),
        )

    first = catalog.list_page(kind="model", limit=2, sort="default")
    assert first["total"] == 3
    assert first["nextCursor"]

    catalog.create_model_profile(
        name="model-earlier",
        display_name="Earlier display name",
        version="1.0.0",
        description="inserted between page requests",
        spec=ModelSpec(
            model="model-earlier",
            endpoint_url="https://api.openai.com/v1/chat/completions",
            credential_ref="env://AGENTKIT_MODEL_API_KEY",
        ),
    )
    second = catalog.list_page(
        kind="model",
        limit=2,
        sort="default",
        cursor=first["nextCursor"],
    )

    combined = [item.resource_id for item in [*first["items"], *second["items"]]]
    expected = [
        item.resource_id
        for item in catalog.list(kind="model", limit=100)
        if item.name != "model-earlier"
    ]
    assert combined == expected
    assert len(combined) == len(set(combined)) == 3


def test_catalog_api_rejects_unknown_sort_field(tmp_path: Path) -> None:
    service = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/catalog/resources",
            params={"sort": "__import__('os').system('false')"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_provider_models_reuse_ksadk_metadata_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _provider_catalog(**_kwargs):
        return [
            {
                "id": "deepseek-v4-vision",
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
                    "id": "deepseek-v4-vision",
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
        current_model="deepseek-v4-vision",
    )

    assert actual_source == "provider"
    assert [item.name for item in actual] == ["deepseek-v4-vision"]
    descriptor = actual[0]
    assert descriptor.source == "provider"
    assert descriptor.contract["metadata"]["context_window_tokens"] == 131072
    assert descriptor.contract["metadata"]["capabilities"]["multimodal_input_image"] is True
    assert descriptor.contract["discovery"]["contextWindow"] == "provider"
    assert descriptor.contract["discovery"]["inputModalities"] == "provider"


@pytest.mark.asyncio
async def test_provider_models_only_expose_proxy_validated_families_and_sort_newest_first(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _provider_catalog(**_kwargs):
        return [
            {"id": "legacy-vision-1"},
            {"id": "glm-5.2"},
            {"id": "glm-5.3"},
            {"id": "qwen3-max"},
            {"id": "kimi-k2.7"},
            {"id": "minimax-m2"},
            {"id": "deepseek-v4-flash"},
        ]

    monkeypatch.setattr(
        "ksadk.studio.resource_catalog.fetch_provider_model_catalog",
        _provider_catalog,
    )
    catalog = _catalog(tmp_path)
    actual, _ = await catalog.discover_provider_models(
        api_base="https://models.example.test/v1",
        api_key="secret",
        current_model="deepseek-v4-flash",
    )

    # IDs are exactly what the upstream provider returned; only eligibility
    # matching treats punctuation as equivalent.
    assert [item.name for item in actual] == [
        "deepseek-v4-flash",
        "glm-5.3",
        "glm-5.2",
        "kimi-k2.7",
        "minimax-m2",
        "qwen3-max",
    ]
    assert all(item.name != "legacy-vision-1" for item in actual)


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
    assert reloaded.get(mcp.resource_id).required_secret_refs == ["env://FILESYSTEM_TOKEN"]
    assert reloaded.get(tool.resource_id).contract["executor"] == "deferred"


def test_delete_resource_removes_model_file_and_listing(tmp_path: Path):
    """回归：model 删除曾静默失败（目录映射 model→model 而非 models）。"""
    catalog = _catalog(tmp_path)
    descriptor = catalog.create_model_profile(
        name="delete-me",
        display_name="Delete Me",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            model="delete-me",
            endpoint_url="https://api.openai.com/v1/chat/completions",
            credential_ref="env://AGENTKIT_MODEL_API_KEY",
        ),
    )
    assert catalog.list(kind="model")
    catalog.delete_resource(descriptor.resource_id)
    assert catalog.list(kind="model") == []
    with pytest.raises(StudioError) as excinfo:
        catalog.delete_resource(descriptor.resource_id)
    assert excinfo.value.status_code == 404


def test_policy_preview_maps_strict_loose_and_custom_approvals(tmp_path: Path):
    catalog = _catalog(tmp_path)
    tools = {item.name: item for item in catalog.list(kind="tool", limit=100)}
    read = CapabilityBinding(resource_id=tools["read_workspace_file"].resource_id)
    write = CapabilityBinding(resource_id=tools["write_workspace_file"].resource_id)

    strict, permissions = catalog.policy_preview(
        AgentBindings(policy_template="strict", tools=[read, write])
    )
    loose, _ = catalog.policy_preview(AgentBindings(policy_template="loose", tools=[read, write]))
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
    assert {tool.name: tool.approval for tool in custom}["write_workspace_file"] == ("never")
    assert permissions == ["workspace:file:read", "workspace:file:write"]


def test_compiler_materializes_bindings_into_immutable_dependencies(tmp_path: Path):
    catalog = _catalog(tmp_path)
    _register_model(catalog)
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
                    CapabilityBinding(resource_id=tools["read_workspace_file"].resource_id),
                    CapabilityBinding(resource_id=tools["write_workspace_file"].resource_id),
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
    assert {tool.name: tool.approval for tool in result.resolved.capabilities.tools}[
        "write_workspace_file"
    ] == "always"
    assert "workspace:file:write" in result.resolved.security.allowed_permissions
    assert result.dependency_lock["model"]["model"] == "glm-5.1"


def test_compiler_ignores_legacy_tool_bindings_for_codex_provider(tmp_path: Path):
    catalog = _catalog(tmp_path)
    _register_model(catalog)
    model = catalog.list(kind="model")[0]
    tool = next(item for item in catalog.list(kind="tool", limit=100))
    draft = AgentDraft(
        metadata=AgentMetadata(id="codex-provider-agent", name="Codex Provider Agent"),
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="plugin",
                provider_ref="plugin://io.ksadk.codex-provider@1.0.0",
            ),
            instructions=Instructions(system="Use Codex native tools."),
            bindings=AgentBindings(
                model_profile_id=model.resource_id,
                tools=[CapabilityBinding(resource_id=tool.resource_id)],
            ),
        ),
    )

    result = AgentCompiler(catalog.workspace, catalog=catalog).compile(draft)

    assert result.resolved.capabilities.tools == []
    assert result.dependency_lock["tools"] == []


def test_compiler_keeps_codex_mcp_native_instead_of_expanding_discovered_tools(
    tmp_path: Path,
):
    catalog = _catalog(tmp_path)
    _register_model(catalog)
    model = catalog.list(kind="model")[0]
    mcp = catalog.create_mcp_server(
        display_name="Search MCP",
        description="Native Codex MCP server",
        server=MCPServerRef(
            name="search",
            version="1.0.0",
            transport="http",
            endpoint_url="https://mcp.example.test/rpc",
        ),
    )
    catalog.save_probe(
        mcp.resource_id,
        result={
            "tools": [
                ToolContract(
                    name="web_search",
                    version="1.0.0",
                    executor="mcp",
                    mcp_server="search",
                ).model_dump(by_alias=True, exclude_none=True, mode="json")
            ]
        },
    )
    draft = AgentDraft(
        metadata=AgentMetadata(id="codex-mcp-agent", name="Codex MCP Agent"),
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="plugin",
                provider_ref="plugin://io.ksadk.codex-provider@1.0.0",
            ),
            instructions=Instructions(system="Use the native MCP server."),
            bindings=AgentBindings(
                model_profile_id=model.resource_id,
                mcp_servers=[CapabilityBinding(resource_id=mcp.resource_id)],
            ),
        ),
    )

    result = AgentCompiler(catalog.workspace, catalog=catalog).compile(draft)

    assert [server["name"] for server in result.resolved.capabilities.mcp_servers] == ["search"]
    assert result.resolved.capabilities.tools == []
    assert result.dependency_lock["tools"] == []


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
