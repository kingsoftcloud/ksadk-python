"""A2A 1.0 AgentCard wire-contract tests.

The public JSON is deliberately generated and reparsed with the pinned official
``a2a-sdk`` protobuf classes.  This prevents KsADK-specific aliases or legacy
0.3 fields from silently escaping through the local A2A surface.
"""

from __future__ import annotations

import json

from a2a.types import AgentCard
from click.testing import CliRunner
from google.protobuf.json_format import MessageToDict, ParseDict

from ksadk.a2a.card import A2A_PROTOCOL_VERSION, build_agent_card
from ksadk.cli.cmd_a2a import a2a

# Snapshot of the current main ``specification/a2a.proto`` AgentCard JSON names.
# The public protocol target for the unreleased 0.8 candidate is A2A main, not
# any legacy 0.3 Card shape.
_A2A_MAIN_AGENT_CARD_FIELDS = {
    "name",
    "description",
    "supportedInterfaces",
    "provider",
    "version",
    "documentationUrl",
    "capabilities",
    "securitySchemes",
    "securityRequirements",
    "defaultInputModes",
    "defaultOutputModes",
    "skills",
    "signatures",
    "iconUrl",
}


def test_agent_card_uses_the_official_a2a_1_wire_shape():
    card = build_agent_card(
        name="research-agent",
        base_url="https://agents.example.com/",
        description="Answers research questions.",
        version="1.2.3",
        skills=["research"],
    )

    payload = MessageToDict(card, preserving_proto_field_name=False)

    assert {
        field.json_name for field in AgentCard.DESCRIPTOR.fields
    } == _A2A_MAIN_AGENT_CARD_FIELDS
    assert payload == {
        "name": "research-agent",
        "description": "Answers research questions.",
        "supportedInterfaces": [
            {
                "url": "https://agents.example.com/a2a/jsonrpc",
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            },
            {
                "url": "https://agents.example.com/a2a/v1",
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            },
        ],
        "version": "1.2.3",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "research",
                "name": "Research",
                "description": "Skill: research",
                "tags": ["research"],
            }
        ],
    }
    assert not {"url", "preferredTransport", "additionalInterfaces"} & payload.keys()
    assert set(payload).issubset(_A2A_MAIN_AGENT_CARD_FIELDS)

    reparsed = ParseDict(payload, AgentCard())
    assert MessageToDict(reparsed, preserving_proto_field_name=False) == payload


def test_a2a_card_cli_emits_the_official_wire_shape(tmp_path):
    (tmp_path / "agentengine.yaml").write_text(
        "name: local-codex\nframework: codex\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        a2a,
        [
            "card",
            str(tmp_path),
            "--url",
            "https://agent.example.com",
            "--name",
            "research-agent",
            "--description",
            "Answers research questions.",
            "--skill",
            "research",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["supportedInterfaces"][0] == {
        "url": "https://agent.example.com/a2a/jsonrpc",
        "protocolBinding": "JSONRPC",
        "protocolVersion": "1.0",
    }
    assert payload["supportedInterfaces"][1] == {
        "url": "https://agent.example.com/a2a/v1",
        "protocolBinding": "HTTP+JSON",
        "protocolVersion": "1.0",
    }
    assert "url" not in payload
    ParseDict(payload, AgentCard())
