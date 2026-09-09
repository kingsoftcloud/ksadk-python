from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from ksadk.detection.detector import FrameworkDetector
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    CapabilityBinding,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
    ToolContract,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.framework_run import FrameworkRunSpecResolver
from ksadk.studio.service import StudioService


def test_runtime_ref_is_an_explicit_per_agent_contract() -> None:
    spec = AgentSpec(
        runtime=RuntimeRef(
            type="langgraph",
            project_path="agents/reviewer/source",
            entry_point="agent.py",
            agent_variable="graph",
        )
    )

    payload = spec.model_dump(by_alias=True, exclude_none=True, mode="json")

    assert payload["runtime"] == {
        "type": "langgraph",
        "projectPath": "agents/reviewer/source",
        "entryPoint": "agent.py",
        "agentVariable": "graph",
        "detection": "declared",
    }


def test_studio_lists_codex_and_framework_agents_in_one_registry(tmp_path: Path) -> None:
    studio = StudioService(
        tmp_path,
        codex_runtime_inspector=lambda _runtime: ("test-sdk", "0.144.4", "0.144.4"),
    )
    studio.create_codex_agent(
        agent_id="codex-reviewer",
        spec=AgentSpec(instructions=Instructions(system="Review code")),
    )
    studio.create_agent(
        agent_id="graph-reviewer",
        name="Graph Reviewer",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/graph-reviewer/source",
                entry_point="agent.py",
                agent_variable="graph",
            )
        ),
    )

    agents = studio.list_agents(limit=20)

    assert [agent.metadata.id for agent in agents] == [
        "codex-reviewer",
        "graph-reviewer",
    ]
    assert studio.agent_runtime_type("codex-reviewer") == "codex"
    assert studio.agent_runtime_type("graph-reviewer") == "langgraph"


def test_codex_creation_grants_only_shipped_provider_host_permission(tmp_path: Path) -> None:
    studio = StudioService(
        tmp_path,
        codex_runtime_inspector=lambda _runtime: ("test-sdk", "0.144.4", "0.144.4"),
    )

    codex = studio.create_studio_agent(
        agent_id="codex-default-permission",
        name="Codex Default Permission",
        spec=AgentSpec(
            runtime=RuntimeRef(type="codex", version="0.144.4"),
            instructions=Instructions(system="Answer the user."),
        ),
    )
    graph = studio.create_studio_agent(
        agent_id="graph-default-permission",
        name="Graph Default Permission",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/graph-default-permission/source",
                entry_point="agent.py",
                agent_variable="graph",
            )
        ),
    )

    assert codex.spec.security.allowed_permissions == ["process:host-user"]
    assert graph.spec.security.allowed_permissions == []


def test_runtime_belongs_to_agent_instead_of_studio_process(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    studio.create_agent(
        agent_id="adk-helper",
        name="ADK Helper",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="adk",
                project_path="agents/adk-helper/source",
                entry_point="agent.py",
            )
        ),
    )

    detail = studio.agent_detail("adk-helper")

    assert detail["draft"].spec.runtime.type == "adk"
    assert not hasattr(studio, "codex_mode")


def test_quick_authoring_generates_detectable_framework_source(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    for runtime_type, variable in (("adk", "root_agent"), ("langgraph", "graph")):
        agent_id = f"{runtime_type}-helper"
        project_path = f"agents/{agent_id}/source"
        studio.create_studio_agent(
            agent_id=agent_id,
            name=f"{runtime_type} helper",
            spec=AgentSpec(
                runtime=RuntimeRef(
                    type=runtime_type,
                    project_path=project_path,
                    entry_point="agent.py",
                    agent_variable=variable,
                ),
                instructions=Instructions(system=f"You are the {runtime_type} helper."),
            ),
        )

        source = tmp_path / project_path
        detected = FrameworkDetector(str(source)).detect()

        assert detected.type.value == runtime_type
        assert detected.entry_point == "agent.py"
        assert detected.agent_variable == variable
        assert (source / ".agentkit-generated").is_file()
        if runtime_type == "langgraph":
            generated = (source / "agent.py").read_text(encoding="utf-8")
            assert "stream_usage=True" in generated
            assert "if not any(isinstance(message, SystemMessage)" in generated


@pytest.mark.parametrize("runtime_type", ["adk", "langgraph"])
def test_updating_bound_model_refreshes_generated_runtime_source(
    tmp_path: Path,
    runtime_type: str,
) -> None:
    studio = StudioService(tmp_path)
    model_a = studio.catalog.create_model_profile(
        name="model-a",
        display_name="Model A",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            model="model-a",
            endpoint_url="https://models.example.test/v1",
            credential_ref="env://MODEL_A_KEY",
        ),
    )
    model_b = studio.catalog.create_model_profile(
        name="model-b",
        display_name="Model B",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            model="model-b",
            endpoint_url="https://models.example.test/v1",
            credential_ref="env://MODEL_B_KEY",
        ),
    )
    draft = studio.create_studio_agent(
        agent_id=f"{runtime_type}-model-edit",
        name="Model Edit",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type=runtime_type,
                project_path=f"agents/{runtime_type}-model-edit/source",
                entry_point="agent.py",
                agent_variable="root_agent" if runtime_type == "adk" else "graph",
            ),
            instructions=Instructions(system="Use the configured model."),
            bindings=AgentBindings(
                model_profile_id=model_a.resource_id,
                model_profile_ids=[model_a.resource_id],
            ),
        ),
    )
    source_path = tmp_path / draft.spec.runtime.project_path / "agent.py"
    assert 'or "model-a"' in source_path.read_text(encoding="utf-8")

    updated_spec = draft.spec.model_copy(deep=True)
    updated_spec.bindings.model_profile_id = model_b.resource_id
    updated_spec.bindings.model_profile_ids = [model_b.resource_id]
    updated = studio.update_studio_agent(
        draft.metadata.id,
        updated_spec,
        expected_revision=draft.metadata.revision,
    )

    assert updated.metadata.revision == draft.metadata.revision + 1
    refreshed = source_path.read_text(encoding="utf-8")
    assert 'or "model-b"' in refreshed
    assert 'or "model-a"' not in refreshed


def test_update_round_trips_unchanged_unresolved_historical_bindings(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="historical-bindings",
        name="Historical Bindings",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/historical-bindings/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="legacy-model",
                endpoint_url="https://models.example.test/v1",
                credential_ref="env://MODEL_KEY",
            ),
            instructions=Instructions(system="Original prompt."),
        ),
    )
    historical = studio.drafts.get(draft.metadata.id)
    historical.spec.bindings = AgentBindings(
        model_profile_id="model:legacy:missing:1",
        model_profile_ids=["model:legacy:missing:1"],
        skills=[CapabilityBinding(resource_id="skill:legacy:missing:1")],
        mcp_servers=[CapabilityBinding(resource_id="mcp:legacy:missing:1")],
        tools=[CapabilityBinding(resource_id="tool:legacy:missing:1")],
    )
    studio.drafts.replace(historical)

    candidate = historical.spec.model_copy(deep=True)
    candidate.instructions.system = "Updated prompt."
    updated = studio.update_studio_agent(
        historical.metadata.id,
        candidate,
        expected_revision=historical.metadata.revision,
    )

    assert updated.metadata.revision == historical.metadata.revision + 1
    assert updated.spec.bindings == historical.spec.bindings
    assert "Updated prompt." in (
        tmp_path / "agents/historical-bindings/source/agent.py"
    ).read_text(encoding="utf-8")


def test_failed_runtime_materialization_does_not_commit_agent_revision(
    tmp_path: Path,
) -> None:
    tool_source = tmp_path / "tools/audit.py"
    tool_source.parent.mkdir(parents=True)
    tool_source.write_text("def audit() -> str:\n    return 'ok'\n", encoding="utf-8")
    studio = StudioService(tmp_path)
    tool = studio.catalog.create_tool(
        display_name="Audit",
        category="custom",
        contract=ToolContract(
            name="audit",
            version="1.0.0",
            executor="python",
            source_path="tools/audit.py",
            callable_name="audit",
        ),
    )
    draft = studio.create_studio_agent(
        agent_id="atomic-update",
        name="Atomic Update",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/atomic-update/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="model-a",
                endpoint_url="https://models.example.test/v1",
                credential_ref="env://MODEL_KEY",
            ),
            instructions=Instructions(system="Original prompt."),
        ),
    )
    source_path = (
        tmp_path / "agents/atomic-update/source/agent.py"
    )
    original_source = source_path.read_text(encoding="utf-8")
    candidate = draft.spec.model_copy(deep=True)
    candidate.instructions.system = "Must not commit."
    candidate.bindings.tools = [CapabilityBinding(resource_id=tool.resource_id)]
    locked_source = tmp_path / str(tool.contract["sourcePath"])
    locked_source.write_text(
        "def audit() -> str:\n    return 'tampered snapshot'\n",
        encoding="utf-8",
    )

    with pytest.raises(StudioError, match="源码与 Catalog 锁定摘要不一致"):
        studio.update_studio_agent(
            draft.metadata.id,
            candidate,
            expected_revision=draft.metadata.revision,
        )

    persisted = studio.drafts.get(draft.metadata.id)
    assert persisted.metadata.revision == draft.metadata.revision
    assert persisted.spec.instructions.system == "Original prompt."
    assert persisted.spec.bindings.tools == []
    assert source_path.read_text(encoding="utf-8") == original_source


def test_framework_update_rejects_capabilities_runtime_source_cannot_inject(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    deferred = studio.catalog.create_tool(
        display_name="Deferred Tool",
        category="custom",
        contract=ToolContract(
            name="deferred_tool",
            version="1.0.0",
            executor="deferred",
        ),
    )
    draft = studio.create_studio_agent(
        agent_id="honest-capabilities",
        name="Honest Capabilities",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="adk",
                project_path="agents/honest-capabilities/source",
                entry_point="agent.py",
                agent_variable="root_agent",
            ),
            model=ModelSpec(
                model="model-a",
                endpoint_url="https://models.example.test/v1",
                credential_ref="env://MODEL_KEY",
            ),
            instructions=Instructions(system="Use supported capabilities only."),
        ),
    )

    with_mcp = draft.spec.model_copy(deep=True)
    with_mcp.bindings.mcp_servers = [
        CapabilityBinding(resource_id="mcp:new:unsupported:1")
    ]
    with pytest.raises(StudioError) as mcp_error:
        studio.update_studio_agent(
            draft.metadata.id,
            with_mcp,
            expected_revision=draft.metadata.revision,
        )
    assert mcp_error.value.code == "MCP_RUNTIME_INCOMPATIBLE"
    assert studio.drafts.get(draft.metadata.id).metadata.revision == draft.metadata.revision

    with_deferred = draft.spec.model_copy(deep=True)
    with_deferred.bindings.tools = [CapabilityBinding(resource_id=deferred.resource_id)]
    with pytest.raises(StudioError) as tool_error:
        studio.update_studio_agent(
            draft.metadata.id,
            with_deferred,
            expected_revision=draft.metadata.revision,
        )
    assert tool_error.value.code == "TOOL_RUNTIME_INCOMPATIBLE"
    assert studio.drafts.get(draft.metadata.id).metadata.revision == draft.metadata.revision


def test_framework_build_runs_from_immutable_source_snapshot(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="graph-helper",
        name="Graph Helper",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/graph-helper/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://OPENAI_API_KEY",
            ),
            instructions=Instructions(system="Answer with evidence."),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    build = studio.builder.build(draft)
    original_source = tmp_path / "agents/graph-helper/source/agent.py"
    original_source.write_text("raise RuntimeError('mutated after build')\n", encoding="utf-8")

    run_spec = FrameworkRunSpecResolver(
        studio.workspace,
        build_repository=studio.builds,
    ).resolve(build.id)

    assert build.runtime_type == "langgraph"
    assert build.source_digest.startswith("sha256:")
    assert run_spec.launch_context.runtime_type == "langgraph"
    assert run_spec.launch_context.project_dir != original_source.parent
    assert "mutated after build" not in (
        run_spec.launch_context.project_dir / "agent.py"
    ).read_text(encoding="utf-8")
    bundle_root = run_spec.launch_context.project_dir.parent
    provenance = json.loads((bundle_root / "provenance.json").read_text())
    lock = json.loads((bundle_root / "agentkit.lock").read_text())
    resolved = json.loads((bundle_root / "resolved-agent-spec.json").read_text())
    assert provenance["resolvedDigest"] == build.resolved_digest
    assert lock["resolvedDigest"] == build.resolved_digest
    assert resolved["resolvedDigest"] == build.resolved_digest
    assert provenance["definitionDigest"] == lock["definitionDigest"]
    with pytest.raises(Exception, match="未绑定"):
        studio.framework_runs.resolve(build.id, model="unbound-model")


@pytest.mark.asyncio
async def test_framework_build_reuses_studio_provider_model_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _provider_catalog(**_kwargs):
        return [{"id": "deepseek-v4-flash", "display_name": "DeepSeek V4 Flash"}]

    monkeypatch.setattr(
        "ksadk.studio.resource_catalog.fetch_provider_model_catalog",
        _provider_catalog,
    )
    studio = StudioService(tmp_path)
    models, source = await studio.catalog.discover_provider_models(
        api_base="https://models.example.test/v1",
        api_key="secret",
        current_model="deepseek-v4-flash",
    )
    draft = studio.create_studio_agent(
        agent_id="live-graph-helper",
        name="Live Graph Helper",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/live-graph-helper/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            instructions=Instructions(system="Answer briefly."),
            bindings=AgentBindings(model_profile_id=models[0].resource_id),
        ),
    )

    build = studio.builder.build(draft)

    assert source == "provider"
    assert build.status == "SUCCEEDED"
    assert build.runtime_type == "langgraph"


@pytest.mark.parametrize(
    ("runtime_type", "agent_variable"),
    (("adk", "root_agent"), ("langgraph", "graph")),
)
def test_generated_framework_binds_and_executes_selected_builtin_tool(
    tmp_path: Path,
    runtime_type: str,
    agent_variable: str,
) -> None:
    studio = StudioService(tmp_path)
    tool = next(
        item
        for item in studio.catalog.list(kind="tool", limit=200)
        if item.name == "component_status"
    )
    draft = studio.create_studio_agent(
        agent_id=f"{runtime_type}-tools",
        name=f"{runtime_type} tools",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type=runtime_type,
                project_path=f"agents/{runtime_type}-tools/source",
                entry_point="agent.py",
                agent_variable=agent_variable,
            ),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://OPENAI_API_KEY",
            ),
            instructions=Instructions(system="Use the bound status tool."),
            bindings=AgentBindings(tools=[CapabilityBinding(resource_id=tool.resource_id)]),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    source = tmp_path / draft.spec.runtime.project_path / "agent.py"
    module_spec = importlib.util.spec_from_file_location(
        f"generated_{runtime_type}_tool_agent",
        source,
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    native_tools = module._langchain_tools if runtime_type == "adk" else module._tools
    assert [tool.name for tool in native_tools] == ["component_status"]
    result = native_tools[0].invoke({})
    assert isinstance(result, dict)
    assert result


def test_generated_langgraph_runtime_exports_a_managed_checkpoint_factory(tmp_path: Path) -> None:
    """Studio source keeps local authoring convenient without fixing production to memory."""
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="managed-checkpoint-graph",
        name="Managed checkpoint graph",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/managed-checkpoint-graph/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://OPENAI_API_KEY",
            ),
            instructions=Instructions(system="Answer concisely."),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )

    source = (tmp_path / draft.spec.runtime.project_path / "agent.py").read_text(
        encoding="utf-8"
    )

    assert "def ksadk_graph_factory(*, checkpointer, resource_tools=()):" in source
    assert "graph = ksadk_graph_factory(checkpointer=MemorySaver())" in source


@pytest.mark.parametrize("runtime_type", ["adk", "langgraph"])
def test_generated_framework_injects_confirmed_skill_instructions(
    tmp_path: Path,
    runtime_type: str,
) -> None:
    skill = tmp_path / "capabilities/skills/evidence"
    skill.mkdir(parents=True)
    (skill / "skill.yaml").write_text(
        "name: evidence\nversion: 1.0.0\ndescription: Cite evidence\n",
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: evidence\ndescription: Cite evidence\n---\n\nAlways cite primary evidence.\n",
        encoding="utf-8",
    )
    studio = StudioService(tmp_path)
    resource = next(
        item for item in studio.catalog.list(kind="skill", limit=100) if item.name == "evidence"
    )
    draft = studio.create_studio_agent(
        agent_id=f"{runtime_type}-skill",
        name="Skill Agent",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type=runtime_type,
                project_path=f"agents/{runtime_type}-skill/source",
                entry_point="agent.py",
                agent_variable="graph" if runtime_type == "langgraph" else "root_agent",
            ),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://OPENAI_API_KEY",
            ),
            instructions=Instructions(system="Answer carefully."),
            bindings=AgentBindings(skills=[CapabilityBinding(resource_id=resource.resource_id)]),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )

    source = (tmp_path / draft.spec.runtime.project_path / "agent.py").read_text(encoding="utf-8")
    assert "Always cite primary evidence." in source
    build = studio.builder.build(draft)
    assert build.runtime_lock["type"] == runtime_type
    assert (
        tmp_path
        / "dist"
        / draft.metadata.id
        / build.id
        / "agent-bundle/capabilities/skills/evidence/SKILL.md"
    ).is_file()


@pytest.mark.parametrize(
    ("runtime_type", "agent_variable"),
    (("adk", "root_agent"), ("langgraph", "graph")),
)
def test_generated_framework_executes_workspace_python_tool_from_build_snapshot(
    tmp_path: Path,
    runtime_type: str,
    agent_variable: str,
) -> None:
    source = tmp_path / "tools/greet.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def greet(name: str) -> dict:\n"
        '    """Return a deterministic greeting."""\n'
        "    return {'message': f'hello {name}'}\n",
        encoding="utf-8",
    )
    studio = StudioService(tmp_path)
    custom = studio.catalog.create_tool(
        display_name="Greeting",
        category="custom",
        contract=ToolContract(
            name="greet",
            version="1.0.0",
            description="Return a deterministic greeting",
            executor="python",
            source_path="tools/greet.py",
            callable_name="greet",
        ),
    )
    draft = studio.create_studio_agent(
        agent_id=f"{runtime_type}-custom-tool",
        name="Custom Tool Agent",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type=runtime_type,
                project_path=f"agents/{runtime_type}-custom-tool/source",
                entry_point="agent.py",
                agent_variable=agent_variable,
            ),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://OPENAI_API_KEY",
            ),
            instructions=Instructions(system="Use greet when asked."),
            bindings=AgentBindings(tools=[CapabilityBinding(resource_id=custom.resource_id)]),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    build = studio.builder.build(draft)
    runtime_root = tmp_path / "dist" / draft.metadata.id / build.id / "agent-bundle/runtime"
    generated = runtime_root / "agent.py"
    assert (runtime_root / ".agentkit_tools/greet.py").is_file()
    module_spec = importlib.util.spec_from_file_location(
        f"built_{runtime_type}_custom_tool_agent",
        generated,
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    native_tools = module._langchain_tools if runtime_type == "adk" else module._tools

    assert [tool.name for tool in native_tools] == ["greet"]
    assert native_tools[0].invoke({"name": "Ada"}) == {"message": "hello Ada"}
