from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import (
    AgentSpec,
    Instructions,
    MCPServerRef,
    ModelSpec,
    RuntimeRef,
    Usage,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import ModelResponse
from ksadk.studio.service import StudioService


def test_generated_agent_slug_retries_collisions(monkeypatch: pytest.MonkeyPatch) -> None:
    from ksadk.studio import identifiers

    values = iter(["00000000", "a1b2c3d4"])
    monkeypatch.setattr(identifiers.secrets, "token_hex", lambda _size: next(values))

    assert (
        identifiers.generate_agent_slug(lambda value: value == "agentkit-00000000")
        == "agentkit-a1b2c3d4"
    )


def _register_model(studio: StudioService) -> None:
    studio.catalog.create_model_profile(
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


class _AuthoringModelClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[list[dict]] = []
        self.calls: list[dict] = []

    async def complete(self, _model, *, messages, **kwargs):
        self.messages.append(messages)
        self.calls.append(kwargs)
        return ModelResponse(
            content=self.content,
            finish_reason="stop",
            usage=Usage(input_tokens=10, output_tokens=10, total_tokens=20),
            tool_calls=[],
            raw_message={"role": "assistant", "content": self.content},
        )


class _SequencedAuthoringModelClient(_AuthoringModelClient):
    def __init__(self, contents: list[str]) -> None:
        super().__init__(contents[0])
        self.contents = list(contents)

    async def complete(self, _model, *, messages, **kwargs):
        self.messages.append(messages)
        self.calls.append(kwargs)
        content = self.contents.pop(0)
        return ModelResponse(
            content=content,
            finish_reason="stop",
            usage=Usage(input_tokens=10, output_tokens=10, total_tokens=20),
            tool_calls=[],
            raw_message={"role": "assistant", "content": content},
        )


def _framework_spec(runtime_type: str, project_path: str) -> AgentSpec:
    return AgentSpec(
        runtime=RuntimeRef(
            type=runtime_type,
            project_path=project_path,
            entry_point="agent.py",
            agent_variable="graph" if runtime_type == "langgraph" else "root_agent",
        ),
        instructions=Instructions(system="Review code with evidence."),
    )


def test_quick_authoring_allocates_stable_id_and_owns_generated_source(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)

    draft = studio.create_authored_agent(
        name="Review Helper",
        slug="review-helper",
        runtime_type="langgraph",
        spec=AgentSpec(instructions=Instructions(system="Review code.")),
    )

    assert re.fullmatch(r"review-helper-[0-9a-f]{12}", draft.metadata.id)
    assert draft.metadata.labels["agentkit.ksyun.com/slug"] == "review-helper"
    assert draft.spec.runtime == RuntimeRef(
        type="langgraph",
        project_path=f"agents/{draft.metadata.id}/source",
        entry_point="agent.py",
        agent_variable="graph",
    )
    assert (tmp_path / draft.spec.runtime.project_path / "agent.py").is_file()


def test_quick_authoring_generates_local_id_when_slug_is_omitted(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)

    draft = studio.create_authored_agent(
        name="Generated Helper",
        runtime_type="adk",
        spec=AgentSpec(instructions=Instructions(system="Help safely.")),
    )

    assert re.fullmatch(r"agentkit-[0-9a-f]{8}", draft.metadata.id)
    assert draft.metadata.labels["agentkit.ksyun.com/slug"] == draft.metadata.id


def test_generated_local_id_is_never_overwritten(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    created = studio.create_authored_agent(
        name="First",
        slug="agentkit-deadbeef",
        runtime_type="adk",
    )
    assert created.metadata.id == "agentkit-deadbeef"

    with pytest.raises(StudioError) as raised:
        studio.create_authored_agent(
            name="Second",
            slug="agentkit-deadbeef",
            runtime_type="adk",
        )
    assert getattr(raised.value, "code", "") == "AGENT_ALREADY_EXISTS"


def test_import_inspect_is_read_only_until_confirmed_commit(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    payload = {
        "apiVersion": "agentkit.ksyun.com/v1alpha1",
        "kind": "Agent",
        "metadata": {"id": "imported-helper", "name": "Imported Helper"},
        "spec": _framework_spec(
            "adk",
            "agents/imported-helper/source",
        ).model_dump(by_alias=True, exclude_none=True, mode="json"),
    }

    inspection = studio.inspect_agent_import(
        yaml.safe_dump(payload, allow_unicode=True).encode(),
        filename="agent.yaml",
    )

    assert inspection["runtimeType"] == "adk"
    assert inspection["requiresConfirmation"] is True
    assert studio.list_agents() == []

    draft = studio.commit_agent_import(
        inspection["inspectionToken"],
        name="Imported Review Helper",
        slug="imported-review-helper",
    )

    assert draft.metadata.id.startswith("imported-review-helper-")
    assert draft.spec.runtime.type == "adk"
    assert (tmp_path / draft.spec.runtime.project_path / "agent.py").is_file()


def test_agent_zip_import_preserves_source_only_after_confirmation(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    payload = {
        "apiVersion": "agentkit.ksyun.com/v1alpha1",
        "kind": "Agent",
        "metadata": {"id": "graph-import", "name": "Graph Import"},
        "spec": _framework_spec(
            "langgraph",
            "source",
        ).model_dump(by_alias=True, exclude_none=True, mode="json"),
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("bundle/agent.yaml", yaml.safe_dump(payload))
        archive.writestr(
            "bundle/source/agent.py",
            "from langgraph.graph import StateGraph\ngraph = StateGraph(dict).compile()\n",
        )
        archive.writestr(
            "bundle/source/ksadk.yaml",
            "framework: langgraph\nentry_point: agent.py\nagent_variable: graph\n",
        )

    inspection = studio.inspect_agent_import(stream.getvalue(), filename="agent.zip")
    assert "bundle/source/agent.py" in inspection["files"]
    assert not list((tmp_path / "agents").glob("*/source/agent.py"))

    draft = studio.commit_agent_import(inspection["inspectionToken"])
    assert re.fullmatch(r"agentkit-[0-9a-f]{8}", draft.metadata.id)
    source = tmp_path / draft.spec.runtime.project_path / "agent.py"
    assert "StateGraph" in source.read_text()
    assert not (source.parent / ".agentkit-generated").exists()


def test_project_detection_requires_inspect_then_commit_and_never_rewrites_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "projects/existing-graph"
    project.mkdir(parents=True)
    source = "from langgraph.graph import StateGraph\ngraph = StateGraph(dict).compile()\n"
    (project / "agent.py").write_text(source)
    (project / "ksadk.yaml").write_text(
        "name: existing-graph\nframework: langgraph\nentry_point: agent.py\nagent_variable: graph\n"
    )
    studio = StudioService(tmp_path)

    inspection = studio.inspect_agent_project("projects/existing-graph")
    assert inspection["runtimeType"] == "langgraph"
    assert inspection["confidence"] == 1.0
    assert studio.list_agents() == []

    draft = studio.commit_agent_project(
        inspection["inspectionToken"],
        name="Existing Graph",
        slug="existing-graph",
    )

    assert draft.spec.runtime.project_path == "projects/existing-graph"
    assert (project / "agent.py").read_text() == source
    assert not (project / ".agentkit-generated").exists()


@pytest.mark.asyncio
async def test_conversation_authoring_uses_bound_real_model_and_returns_patch_only(
    tmp_path: Path,
) -> None:
    response = json.dumps(
        {
            "name": "Release Reviewer",
            "slug": "release-reviewer",
            "runtimeType": "adk",
            "description": "Checks release readiness.",
            "spec": {
                "runtime": {
                    "type": "adk",
                    "projectPath": "generated/source",
                    "entryPoint": "main.py",
                    "agentVariable": "root_agent",
                },
                "instructions": {
                    "system": "You review release evidence.",
                    "task": "Return blockers and proof.",
                },
                "model": {
                    "model": "glm-5.1",
                    "baseUrl": "https://models.example.test/v1",
                    "credentialRef": "env://AGENTKIT_MODEL_API_KEY",
                    "parameters": {"temperature": 0.1, "maxTokens": 8192},
                },
                "bindings": {
                    "modelParameters": {"temperature": 0.3, "maxTokens": 4096},
                    "policyTemplate": "custom",
                    "tools": [{"resourceId": "tool-release-check", "approval": "policy"}],
                    "mcpServers": [{"resourceId": "mcp-release"}],
                    "skills": [{"resourceId": "skill-release"}],
                },
                "execution": {
                    "strategy": "plan-act-observe",
                    "maxSteps": 24,
                    "timeoutSeconds": 300,
                },
                "context": {
                    "ownership": "framework",
                    "maxInputTokens": 64000,
                    "reserveOutputTokens": 4096,
                },
                "memory": {"enabled": True, "providerRef": "memory-release"},
                "security": {"toolPolicy": "allow-listed", "allowedPermissions": ["repo:read"]},
                "evaluation": {"suiteRefs": ["release-gate"], "minimumPassRate": 0.9},
            },
        }
    )
    model_client = _AuthoringModelClient(response)
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    proposal = await studio.compose_agent_conversation(
        messages=[
            {"role": "user", "content": "做一个发布评审 Agent"},
            {"role": "assistant", "content": "你希望用哪个 Runtime？"},
            {"role": "user", "content": "ADK，输出阻断项和证据"},
        ],
        model_profile_id=model_profile.resource_id,
    )

    assert proposal["proposal"]["runtimeType"] == "adk"
    spec = proposal["proposal"]["spec"]
    assert spec["instructions"]["system"] == "You review release evidence."
    assert spec["runtime"]["entryPoint"] == "main.py"
    assert spec["model"]["parameters"]["maxTokens"] == 8192
    assert spec["bindings"]["tools"] == [
        {"resourceId": "tool-release-check", "enabled": True, "approval": "policy", "config": {}}
    ]
    assert spec["execution"]["strategy"] == "plan-act-observe"
    assert spec["context"]["maxInputTokens"] == 64000
    assert spec["memory"]["providerRef"] == "memory-release"
    assert spec["security"]["allowedPermissions"] == ["repo:read"]
    assert spec["evaluation"]["suiteRefs"] == ["release-gate"]
    assert proposal["requiresConfirmation"] is True
    assert proposal["usage"]["reported"] is False
    assert studio.list_agents() == []
    assert model_client.messages[0][-1]["content"].endswith("ADK，输出阻断项和证据")


@pytest.mark.asyncio
async def test_conversation_authoring_merges_a_partial_follow_up_patch(
    tmp_path: Path,
) -> None:
    previous = {
        "name": "Release Reviewer",
        "slug": "release-reviewer",
        "runtimeType": "adk",
        "description": "Checks release readiness.",
        "spec": {
            "runtime": {
                "type": "adk",
                "projectPath": "generated/source",
                "entryPoint": "main.py",
                "agentVariable": "root_agent",
            },
            "instructions": {
                "system": "You review release evidence.",
                "task": "Return blockers and proof.",
            },
            "bindings": {
                "modelProfileId": "model/glm-5.1",
                "skills": [{"resourceId": "skill-release"}],
            },
        },
    }
    model_client = _AuthoringModelClient(
        "我只修改任务要求：\n```json\n"
        + json.dumps(
            {
                "description": "Checks release readiness and rollback safety.",
                "spec": {
                    "instructions": {"task": "Return blockers, proof, and rollback steps."}
                },
            }
        )
        + "\n```"
    )
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    proposal = await studio.compose_agent_conversation(
        messages=[
            {"role": "user", "content": "做一个发布评审 Agent"},
            {"role": "assistant", "content": json.dumps(previous)},
            {"role": "user", "content": "再补充回滚安全检查"},
        ],
        model_profile_id=model_profile.resource_id,
    )

    result = proposal["proposal"]
    assert result["name"] == "Release Reviewer"
    assert result["runtimeType"] == "adk"
    assert result["description"] == "Checks release readiness and rollback safety."
    assert result["spec"]["runtime"]["entryPoint"] == "main.py"
    assert result["spec"]["bindings"]["skills"] == [
        {
            "resourceId": "skill-release",
            "enabled": True,
            "approval": None,
            "config": {},
        }
    ]
    assert result["spec"]["instructions"] == {
        "system": "You review release evidence.",
        "task": "Return blockers, proof, and rollback steps.",
    }


@pytest.mark.asyncio
async def test_conversation_authoring_retries_one_invalid_model_patch(
    tmp_path: Path,
) -> None:
    valid = json.dumps(
        {
            "name": "Release Reviewer",
            "slug": "release-reviewer",
            "runtimeType": "codex",
            "description": "Checks releases.",
            "spec": {
                "instructions": {
                    "system": "Review releases.",
                    "task": "Return evidence.",
                }
            },
        }
    )
    model_client = _SequencedAuthoringModelClient(["not-json", valid])
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    proposal = await studio.compose_agent_conversation(
        messages=[{"role": "user", "content": "做一个发布评审 Agent"}],
        model_profile_id=model_profile.resource_id,
    )

    assert proposal["proposal"]["name"] == "Release Reviewer"
    assert len(model_client.messages) == 2
    assert "上一次输出未通过 Agent Draft Patch 校验" in model_client.messages[1][-1]["content"]


def test_conversation_prompt_only_response_is_migrated_to_complete_spec(tmp_path: Path) -> None:
    from ksadk.studio.authoring import AgentAuthoringService

    proposal = AgentAuthoringService.parse_conversation_proposal(json.dumps({
        "name": "Legacy Helper",
        "slug": "legacy-helper",
        "runtimeType": "codex",
        "description": "Legacy response",
        "instructions": {"system": "Keep this prompt.", "task": "Keep this task."},
    }))

    assert proposal.spec.description == "Legacy response"
    assert proposal.spec.instructions == Instructions(
        system="Keep this prompt.",
        task="Keep this task.",
    )


def test_codex_manifest_import_uses_canonical_lossless_projection(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    _register_model(studio)
    mcp = studio.catalog.create_mcp_server(
        display_name="Release MCP",
        description="",
        server=MCPServerRef(
            name="release-mcp",
            version="1.0.0",
            transport="http",
            endpoint_url="https://mcp.example.test/rpc",
        ),
    )
    payload = {
        "name": "codex-import",
        "version": "2.1.0",
        "framework": "codex",
        "artifact_type": "ManagedRuntime",
        "runtime": {"name": "codex", "version": "0.147.0"},
        "model": "glm-5.1",
        "models": ["glm-5.1"],
        "prompt": "Review safely.",
        "task_prompt": "Return evidence.",
        "skills": ["skill-release-review"],
        "mcp_servers": [{"name": "release-mcp", "url": "https://mcp.example.test/rpc"}],
        "context": {"ownership": "native", "maxInputTokens": 64000, "reserveOutputTokens": 4096},
        "memory": {"enabled": True, "providerRef": "memory-release"},
    }
    inspection = studio.inspect_agent_import(
        yaml.safe_dump(payload, allow_unicode=True).encode(),
        filename="agentengine.yaml",
    )

    draft = studio.commit_agent_import(
        inspection["inspectionToken"],
        name="Imported Codex",
        slug="imported-codex",
    )

    saved = studio.codex_manifests.load(draft.metadata.id).manifest
    assert saved.task_prompt == "Return evidence."
    assert saved.skills == ["skill-release-review"]
    assert saved.mcp_servers == [{"name": "release-mcp", "url": "https://mcp.example.test/rpc"}]
    assert saved.context and saved.context.ownership == "native"
    assert saved.memory and saved.memory.provider_ref == "memory-release"
    assert draft.spec.instructions.task == "Return evidence."
    assert [item.resource_id for item in draft.spec.bindings.skills] == ["skill-release-review"]
    assert [item.resource_id for item in draft.spec.bindings.mcp_servers] == [mcp.resource_id]
    assert draft.spec.context.ownership == "native"
    assert draft.spec.memory.provider_ref == "memory-release"


@pytest.mark.parametrize(
    ("runtime_type", "agent_variable"),
    [("adk", "root_agent"), ("langgraph", "graph")],
)
def test_project_import_round_trips_complete_agent_spec(
    tmp_path: Path,
    runtime_type: str,
    agent_variable: str,
) -> None:
    studio = StudioService(tmp_path)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]
    project = tmp_path / f"projects/{runtime_type}-complete"
    project.mkdir(parents=True)
    (project / "agent.py").write_text(f"{agent_variable} = object()\n")
    config = {
        "name": f"{runtime_type}-complete",
        "framework": runtime_type,
        "entry_point": "agent.py",
        "agent_variable": agent_variable,
        "spec": {
            "description": "Complete imported project",
            "instructions": {"system": "Keep system.", "task": "Keep task."},
            "model": {
                "model": "glm-5.1",
                "baseUrl": "https://models.example.test/v1",
                "credentialRef": "env://MODEL_KEY",
                "parameters": {"temperature": 0.4, "maxTokens": 6000},
            },
            "bindings": {
                "modelProfileId": model_profile.resource_id,
                "modelProfileIds": [model_profile.resource_id],
                "modelParameters": {"temperature": 0.5, "maxTokens": 5000},
                "policyTemplate": "custom",
            },
            "capabilities": {
                "skills": [{"name": "review", "version": "1.2.0"}],
                "mcpServers": [
                    {
                        "name": "docs",
                        "version": "1.0.0",
                        "transport": "http",
                        "endpointUrl": "https://mcp.example.test",
                    }
                ],
                "tools": [{"name": "inspect", "version": "1.0.0", "executor": "builtin"}],
            },
            "execution": {"strategy": "plan-act-observe", "maxSteps": 22, "timeoutSeconds": 240},
            "context": {
                "ownership": "framework",
                "maxInputTokens": 48000,
                "reserveOutputTokens": 4096,
            },
            "memory": {"enabled": True, "providerRef": "memory-project"},
            "security": {"toolPolicy": "allow-listed", "allowedPermissions": ["project:read"]},
            "evaluation": {"suiteRefs": ["project-suite"], "minimumPassRate": 0.85},
        },
    }
    (project / "ksadk.yaml").write_text(yaml.safe_dump(config, allow_unicode=True))

    inspection = studio.inspect_agent_project(f"projects/{runtime_type}-complete")
    assert inspection["bindingProjection"]["unresolved"] == []
    draft = studio.commit_agent_project(
        inspection["inspectionToken"],
        name="Complete Project",
        slug=f"{runtime_type}-complete",
        model_profile_id=model_profile.resource_id,
    )

    assert draft.spec.runtime and draft.spec.runtime.type == runtime_type
    assert draft.spec.runtime.project_path == f"projects/{runtime_type}-complete"
    assert draft.spec.instructions.task == "Keep task."
    assert draft.spec.model and draft.spec.model.parameters.max_tokens == 6000
    assert draft.spec.bindings.model_parameters
    assert draft.spec.bindings.model_parameters.max_tokens == 5000
    assert draft.spec.bindings.policy_template == "custom"
    assert draft.spec.capabilities.skills[0].name == "review"
    assert draft.spec.capabilities.mcp_servers[0].name == "docs"
    assert draft.spec.capabilities.tools[0].name == "inspect"
    assert draft.spec.execution.max_steps == 22
    assert draft.spec.context.max_input_tokens == 48000
    assert draft.spec.memory.provider_ref == "memory-project"
    assert draft.spec.security.allowed_permissions == ["project:read"]
    assert draft.spec.evaluation.suite_refs == ["project-suite"]


def test_project_import_reports_unresolved_bindings_instead_of_dropping_them(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    project = tmp_path / "projects/unresolved-adk"
    project.mkdir(parents=True)
    (project / "agent.py").write_text("root_agent = object()\n")
    (project / "ksadk.yaml").write_text(yaml.safe_dump({
        "name": "unresolved-adk",
        "framework": "adk",
        "entry_point": "agent.py",
        "agent_variable": "root_agent",
        "tools": [{"legacy": "opaque-tool-config"}],
        "skills": ["skill-not-installed"],
    }))

    inspection = studio.inspect_agent_project("projects/unresolved-adk")

    assert inspection["bindingProjection"]["unresolved"] == [
        {
            "kind": "tools",
            "value": {"legacy": "opaque-tool-config"},
            "reason": "unsupported-binding-shape",
        },
        {
            "kind": "skill",
            "value": {"resourceId": "skill-not-installed", "enabled": True, "config": {}},
            "reason": "not-in-resource-catalog",
        },
    ]
    with pytest.raises(StudioError) as raised:
        studio.commit_agent_project(
            inspection["inspectionToken"],
            name="Unresolved",
            slug="unresolved-adk",
        )
    assert raised.value.code == "PROJECT_BINDINGS_UNRESOLVED"


def test_authoring_api_exposes_four_real_modes(tmp_path: Path) -> None:
    model_client = _AuthoringModelClient(
        json.dumps(
            {
                "name": "Conversation Agent",
                "slug": "conversation-agent",
                "runtimeType": "codex",
                "description": "Built through conversation.",
                "instructions": {"system": "Help reliably.", "task": "Answer."},
            }
        )
    )
    service = StudioService(tmp_path, model_client=model_client)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        quick = client.post(
            "/api/v1/authoring/quick",
            json={
                "name": "Quick Agent",
                "slug": "quick-agent",
                "runtimeType": "adk",
                "spec": {"instructions": {"system": "Help.", "task": ""}},
            },
        )
        assert quick.status_code == 201
        assert quick.json()["spec"]["runtime"]["type"] == "adk"

        generated = client.post(
            "/api/v1/authoring/quick",
            json={
                "name": "Generated Agent",
                "runtimeType": "codex",
                "spec": {"instructions": {"system": "Help.", "task": ""}},
            },
        )
        assert generated.status_code == 201
        assert re.fullmatch(r"agentkit-[0-9a-f]{8}", generated.json()["metadata"]["id"])

        _register_model(service)
        model_profile = service.catalog.list(kind="model")[0]
        conversation = client.post(
            "/api/v1/authoring/conversations:compose",
            json={
                "modelProfileId": model_profile.resource_id,
                "messages": [{"role": "user", "content": "做一个问答 Agent"}],
            },
        )
        assert conversation.status_code == 200
        assert conversation.json()["requiresConfirmation"] is True

        project = tmp_path / "projects/api-graph"
        project.mkdir(parents=True)
        (project / "agent.py").write_text(
            "from langgraph.graph import StateGraph\ngraph = StateGraph(dict).compile()\n"
        )
        (project / "ksadk.yaml").write_text(
            "framework: langgraph\nentry_point: agent.py\nagent_variable: graph\n"
        )
        detected = client.post(
            "/api/v1/authoring/projects:inspect",
            json={"path": "projects/api-graph"},
        )
        assert detected.status_code == 200
        committed = client.post(
            f"/api/v1/authoring/projects/{detected.json()['inspectionToken']}:commit",
            json={"name": "API Graph", "slug": "api-graph"},
        )
        assert committed.status_code == 201

        manifest = {
            "apiVersion": "agentkit.ksyun.com/v1alpha1",
            "kind": "Agent",
            "metadata": {"id": "api-import", "name": "API Import"},
            "spec": _framework_spec("adk", "agents/api-import/source").model_dump(
                by_alias=True, exclude_none=True, mode="json"
            ),
        }
        inspected = client.post(
            "/api/v1/authoring/imports:inspect",
            files={
                "file": (
                    "agent.yaml",
                    yaml.safe_dump(manifest).encode(),
                    "application/yaml",
                )
            },
        )
        assert inspected.status_code == 200
        imported = client.post(
            f"/api/v1/authoring/imports/{inspected.json()['inspectionToken']}:commit",
            json={"name": "API Import", "slug": "api-import"},
        )
        assert imported.status_code == 201


@pytest.mark.asyncio
async def test_conversation_authoring_requests_json_object_response(
    tmp_path: Path,
) -> None:
    valid = json.dumps(
        {
            "name": "Release Reviewer",
            "slug": "release-reviewer",
            "runtimeType": "codex",
            "description": "Checks releases.",
            "spec": {
                "instructions": {"system": "Review releases.", "task": "Return evidence."}
            },
        }
    )
    model_client = _AuthoringModelClient(valid)
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    await studio.compose_agent_conversation(
        messages=[{"role": "user", "content": "做一个发布评审 Agent"}],
        model_profile_id=model_profile.resource_id,
    )

    assert model_client.calls[0]["response_format"] == {"type": "json_object"}
    assert model_client.calls[0]["timeout_seconds"] <= 30


@pytest.mark.asyncio
async def test_conversation_authoring_corrective_retry_is_single_attempt(
    tmp_path: Path,
) -> None:
    valid = json.dumps(
        {
            "name": "Release Reviewer",
            "slug": "release-reviewer",
            "runtimeType": "codex",
            "description": "Checks releases.",
            "spec": {
                "instructions": {"system": "Review releases.", "task": "Return evidence."}
            },
        }
    )
    model_client = _SequencedAuthoringModelClient(["not-json", valid])
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    await studio.compose_agent_conversation(
        messages=[{"role": "user", "content": "做一个发布评审 Agent"}],
        model_profile_id=model_profile.resource_id,
    )

    assert len(model_client.calls) == 2
    assert model_client.calls[0]["max_attempts"] == 2
    assert model_client.calls[1]["max_attempts"] == 1


@pytest.mark.asyncio
async def test_conversation_authoring_surfaces_validation_error_details(
    tmp_path: Path,
) -> None:
    model_client = _SequencedAuthoringModelClient(["not-json", "still-not-json"])
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    with pytest.raises(StudioError) as captured:
        await studio.compose_agent_conversation(
            messages=[{"role": "user", "content": "做一个发布评审 Agent"}],
            model_profile_id=model_profile.resource_id,
        )

    assert captured.value.code == "AUTHORING_MODEL_OUTPUT_INVALID"
    assert captured.value.details.get("validationError")


def _valid_conversation_proposal() -> str:
    return json.dumps(
        {
            "name": "Stage Agent",
            "slug": "stage-agent",
            "runtimeType": "codex",
            "description": "Tracks authoring stages.",
            "instructions": {"system": "Help reliably.", "task": "Answer."},
        }
    )


@pytest.mark.asyncio
async def test_conversation_authoring_records_stage_progress(tmp_path: Path) -> None:
    model_client = _SequencedAuthoringModelClient(["not-json", _valid_conversation_proposal()])
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    assert studio.conversation_authoring_status("req-stages") is None

    await studio.compose_agent_conversation(
        messages=[{"role": "user", "content": "做一个阶段跟踪 Agent"}],
        model_profile_id=model_profile.resource_id,
        request_id="req-stages",
    )

    status = studio.conversation_authoring_status("req-stages")
    assert status is not None
    assert status["requestId"] == "req-stages"
    assert status["stage"] == "done"
    assert status["updatedAt"] > 0


@pytest.mark.asyncio
async def test_conversation_authoring_records_failed_stage(tmp_path: Path) -> None:
    model_client = _SequencedAuthoringModelClient(["not-json", "still-not-json"])
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    with pytest.raises(StudioError):
        await studio.compose_agent_conversation(
            messages=[{"role": "user", "content": "做一个 Agent"}],
            model_profile_id=model_profile.resource_id,
            request_id="req-failed",
        )

    status = studio.conversation_authoring_status("req-failed")
    assert status is not None
    assert status["stage"] == "failed"


@pytest.mark.asyncio
async def test_conversation_authoring_emits_start_and_finish_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    model_client = _AuthoringModelClient(_valid_conversation_proposal())
    studio = StudioService(tmp_path, model_client=model_client)
    _register_model(studio)
    model_profile = studio.catalog.list(kind="model")[0]

    with caplog.at_level(logging.INFO, logger="ksadk.studio.authoring_coordinator"):
        await studio.compose_agent_conversation(
            messages=[{"role": "user", "content": "做一个日志 Agent"}],
            model_profile_id=model_profile.resource_id,
            request_id="req-logs",
        )

    messages = [record.message for record in caplog.records]
    assert any(
        message.startswith("conversation authoring started") for message in messages
    )
    assert any(
        message.startswith("conversation authoring finished") for message in messages
    )
    assert any(
        message.startswith("conversation authoring model resolved") for message in messages
    )


def test_conversation_authoring_status_endpoint(tmp_path: Path) -> None:
    model_client = _AuthoringModelClient(_valid_conversation_proposal())
    service = StudioService(tmp_path, model_client=model_client)
    _register_model(service)
    model_profile = service.catalog.list(kind="model")[0]
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        unknown = client.get("/api/v1/authoring/conversations:status/req-missing")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "AUTHORING_STATUS_NOT_FOUND"

        composed = client.post(
            "/api/v1/authoring/conversations:compose",
            json={
                "modelProfileId": model_profile.resource_id,
                "requestId": "req-endpoint",
                "messages": [{"role": "user", "content": "做一个问答 Agent"}],
            },
        )
        assert composed.status_code == 200

        status = client.get("/api/v1/authoring/conversations:status/req-endpoint")
        assert status.status_code == 200
        payload = status.json()
        assert payload["requestId"] == "req-endpoint"
        assert payload["stage"] == "done"
        assert payload["updatedAt"] > 0
