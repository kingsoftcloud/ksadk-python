"""Public and internal Interaction/v1 contract seams.

The fixtures in this test are independent, protocol-level examples.  They do
not reach into the Kernel implementation, so every repository can consume the
same frozen JSON contract before its own implementation lands.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError


CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "agent-kernel" / "v1"


def _schema() -> dict:
    return json.loads((CONTRACT_DIR / "interaction.schema.json").read_text())


def _fixture(name: str):
    return json.loads((CONTRACT_DIR / "fixtures" / name).read_text())


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema(), format_checker=FormatChecker())


def test_public_submission_is_minimal_and_validates_independently():
    payload = _fixture("interaction-submit.json")

    _validator().validate(payload)

    assert set(payload) == {
        "schema_version",
        "interaction_id",
        "expected_revision",
        "action",
        "response",
        "idempotency_key",
    }


@pytest.mark.parametrize("missing", ["expected_revision", "idempotency_key"])
def test_public_submission_rejects_required_compare_and_set_fields(missing: str):
    payload = copy.deepcopy(_fixture("interaction-submit.json"))
    del payload[missing]

    with pytest.raises(ValidationError):
        _validator().validate(payload)


@pytest.mark.parametrize(
    "sensitive_field",
    ["authorization_ref", "permit", "checkpoint", "native_handle", "secret"],
)
def test_public_submission_cannot_carry_internal_authorization_or_runtime_state(
    sensitive_field: str,
):
    payload = copy.deepcopy(_fixture("interaction-submit.json"))
    payload[sensitive_field] = "must-not-cross-the-public-boundary"

    with pytest.raises(ValidationError):
        _validator().validate(payload)


def test_interaction_event_fixtures_cover_the_complete_lifecycle():
    requested_events = _fixture("interaction-requested.json")
    resolved_events = _fixture("interaction-resolved.json")
    events = [*requested_events, *resolved_events]

    for event in events:
        _validator().validate(event)

    assert {event["event_type"] for event in events} == {
        "interaction.requested",
        "interaction.resolved",
        "interaction.cancelled",
        "interaction.expired",
    }
    rejected = next(event for event in events if event.get("outcome") == "rejected")
    assert rejected["event_type"] == "interaction.resolved"


def test_interaction_command_is_internal_and_carries_server_derived_identity():
    schema = _schema()
    command = {
        "schema_version": 1,
        "command_id": "0198b7c4-834a-73e9-99a5-000000000001",
        "tenant_id": "tenant-1",
        "agent_instance_id": "agent-instance-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "interaction_id": "int-1",
        "expected_revision": 1,
        "action": "approve",
        "response": {"approved": True},
        "idempotency_key": "interaction:int-1:revision-1",
        "actor": {"subject_ref": "account:123", "kind": "user"},
        "authorization_ref": "server-permit-reference",
    }

    command_schema = {"$ref": "#/$defs/interactionCommand", "$defs": schema["$defs"]}
    Draft202012Validator(command_schema, format_checker=FormatChecker()).validate(command)
