from __future__ import annotations

import pytest
from pydantic import ValidationError

from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    ContextSpec,
    ModelSpec,
    ToolContract,
)


def test_contracts_serialize_camel_case_without_nulls():
    draft = AgentDraft(
        metadata=AgentMetadata(id="demo-agent", name="Demo"),
        spec=AgentSpec(
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            )
        ),
    )

    payload = draft.model_dump(by_alias=True, exclude_none=True, mode="json")

    assert payload["apiVersion"] == "agentkit.ksyun.com/v1alpha1"
    assert payload["metadata"]["revision"] == 1
    assert payload["spec"]["model"]["endpointUrl"].endswith("/chat/completions")
    assert "baseUrl" not in payload["spec"]["model"]
    assert payload["spec"]["context"]["reserveOutputTokens"] == 4096


@pytest.mark.parametrize(
    ("endpoint_url", "base_url"),
    [
        (None, None),
        (
            "https://model.example.com/v1/chat/completions",
            "https://model.example.com/v1",
        ),
    ],
)
def test_model_requires_exactly_one_address(endpoint_url, base_url):
    with pytest.raises(ValidationError, match="必须且只能配置一个"):
        ModelSpec(
            model="glm-5.1",
            endpoint_url=endpoint_url,
            base_url=base_url,
            credential_ref="env://MODEL_API_KEY",
        )


@pytest.mark.parametrize("agent_id", ["A-demo", "ab", "demo_agent", "../demo"])
def test_agent_id_is_path_safe(agent_id):
    with pytest.raises(ValidationError):
        AgentMetadata(id=agent_id, name="Demo")


def test_context_reserves_less_than_input_budget():
    with pytest.raises(ValidationError, match="必须小于"):
        ContextSpec(max_input_tokens=1024, reserve_output_tokens=1024)


def test_tool_contract_rejects_invalid_timeout():
    with pytest.raises(ValidationError):
        ToolContract(name="echo", version="1.0.0", timeout_seconds=0)


def test_tool_contract_accepts_camel_case_per_run_quota():
    tool = ToolContract.model_validate(
        {"name": "search", "version": "1", "maxCallsPerRun": 15}
    )
    assert tool.max_calls_per_run == 15
    assert tool.model_dump(by_alias=True)["maxCallsPerRun"] == 15
    with pytest.raises(ValidationError):
        ToolContract(name="search", version="1", max_calls_per_run=0)
