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
from ksadk.studio.contracts import AgentSpec, Instructions, ModelSpec, RuntimeRef, Usage
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

    async def complete(self, _model, *, messages, **_kwargs):
        self.messages.append(messages)
        return ModelResponse(
            content=self.content,
            finish_reason="stop",
            usage=Usage(input_tokens=10, output_tokens=10, total_tokens=20),
            tool_calls=[],
            raw_message={"role": "assistant", "content": self.content},
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
            "runtimeType": "agentkit",
            "description": "Checks release readiness.",
            "instructions": {
                "system": "You review release evidence.",
                "task": "Return blockers and proof.",
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
            {"role": "user", "content": "输出阻断项和证据"},
        ],
        model_profile_id=model_profile.resource_id,
    )

    assert proposal["proposal"]["runtimeType"] == "agentkit"
    assert proposal["proposal"]["instructions"]["system"] == "You review release evidence."
    assert proposal["requiresConfirmation"] is True
    assert proposal["usage"]["reported"] is False
    assert studio.list_agents() == []
    assert model_client.messages[0][-1]["content"].endswith("输出阻断项和证据")


def test_authoring_api_exposes_four_real_modes(tmp_path: Path) -> None:
    model_client = _AuthoringModelClient(
        json.dumps(
            {
                "name": "Conversation Agent",
                "slug": "conversation-agent",
                "runtimeType": "agentkit",
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
