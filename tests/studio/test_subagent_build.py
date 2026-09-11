import pytest
from pydantic import ValidationError

from ksadk.harness.spec import SubAgentBinding
from ksadk.studio.compiler import AgentCompiler
from ksadk.studio.contracts import AgentDraft, AgentMetadata, AgentSpec
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace


def draft(children):
    return AgentDraft(
        metadata=AgentMetadata(id="review-agent", name="Review"),
        spec=AgentSpec.model_validate(
            {
                "runtime": {"type": "harness"},
                "instructions": {"system": "Delegate review."},
                "model": {
                    "model": "test-model",
                    "endpointUrl": "https://example.test/v1/chat/completions",
                    "credentialRef": "env://UNUSED_TEST_MODEL_KEY",
                },
                "security": {"network": {"mode": "restricted", "allowedHosts": ["example.test"]}},
                "subAgents": children,
            }
        ),
    )


def test_studio_compile_preserves_typed_child_and_exact_digest(tmp_path):
    compiler = AgentCompiler(Workspace(tmp_path))
    declaration = {
        "name": "review_helper",
        "instructions": "Review the delegated evidence.",
        "tools": [],
        "maxTurns": 3,
        "timeoutSeconds": 30,
        "inheritSkills": False,
    }
    compiled = compiler.compile(draft([declaration]))
    payload = compiled.resolved.model_dump(by_alias=True, mode="json")
    assert payload["subAgents"][0]["maxTurns"] == 3
    child = SubAgentBinding.model_validate(payload["subAgents"][0])
    assert child.name == "review_helper" and child.max_turns == 3
    assert not child.inherit_skills
    changed = compiler.compile(draft([{**declaration, "instructions": "Different instruction"}]))
    assert compiled.resolved.resolved_digest != changed.resolved.resolved_digest
    assert compiled.resolved.source_digest != changed.resolved.source_digest


def test_studio_child_cannot_add_unbound_tool_or_bypass_contract(tmp_path):
    with pytest.raises(StudioError) as error:
        AgentCompiler(Workspace(tmp_path)).compile(
            draft(
                [
                    {
                        "name": "child",
                        "instructions": "Try a write",
                        "tools": ["write_workspace_file"],
                    }
                ]
            )
        )
    assert error.value.code == "SUBAGENT_TOOL_SCOPE_INVALID"
    with pytest.raises(ValidationError, match="依赖环"):
        draft([{"name": "child", "instructions": "Review", "dependsOn": ["child"]}])
    with pytest.raises(ValidationError, match="名称不能重复"):
        draft([{"name": "child", "instructions": "Review"}] * 2)
    with pytest.raises(ValidationError):
        draft([{"name": "child", "instructions": "Review", "arbitraryPermission": "all"}])
