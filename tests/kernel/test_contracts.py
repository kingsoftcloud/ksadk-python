# Agent Kernel v1 合同测试：round-trip、未知字段保留、判别 payload 与 receipt/event 约束。
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from ksadk.kernel.contracts import (
    ActivationLease,
    AgentControlCommand,
    AgentControlPermit,
    AgentControlReceipt,
    AgentStatusSnapshot,
    RuntimeCapability,
    RuntimeCapabilityMatrix,
    SessionEventEnvelope,
    WireModel,
)

FIXTURES_DIR = (
    Path(__file__).resolve().parents[2] / "contracts" / "agent-kernel" / "v1" / "fixtures"
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def load_fixture_list(name: str) -> list[dict]:
    data = load_fixture(name)
    assert isinstance(data, list)
    return data


# ---------------------------------------------------------------- round-trip


def test_unknown_optional_fields_round_trip_without_loss():
    raw = load_fixture("agent-control-enqueue.json") | {"future_hint": {"x": 1}}
    parsed = AgentControlCommand.model_validate(raw)
    assert parsed.model_dump(mode="json")["future_hint"] == {"x": 1}


def test_command_round_trip_preserves_all_fields():
    raw = load_fixture("agent-control-enqueue.json")
    parsed = AgentControlCommand.model_validate(raw)
    dumped = parsed.model_dump(mode="json")
    for key, value in raw.items():
        assert dumped[key] == value


def test_permit_round_trip_preserves_extra_fields():
    raw = load_fixture("agent-control-permit.json")[0] | {"future_flag": True}
    parsed = AgentControlPermit.model_validate(raw)
    assert parsed.model_dump(mode="json")["future_flag"] is True


# ------------------------------------------------------- payload 判别与校验


@pytest.mark.parametrize(
    "name,command_type",
    [
        ("agent-control-enqueue.json", "enqueue"),
        ("agent-control-steer.json", "steer"),
        ("agent-control-inject.json", "inject"),
        ("agent-control-interrupt.json", "interrupt"),
        ("agent-control-pause.json", "pause"),
        ("agent-control-resume.json", "resume"),
        ("agent-control-submit_interaction.json", "submit_interaction"),
    ],
)
def test_all_seven_command_payloads_validate(name, command_type):
    parsed = AgentControlCommand.model_validate(load_fixture(name))
    assert parsed.command_type == command_type


def test_seven_command_fixture_array_validates():
    for raw in load_fixture_list("agent-control.json"):
        AgentControlCommand.model_validate(raw)


def test_enqueue_requires_content():
    raw = load_fixture("agent-control-enqueue.json")
    raw["payload"] = {"reply_to": "q-1"}
    with pytest.raises(ValidationError):
        AgentControlCommand.model_validate(raw)


@pytest.mark.parametrize("field", ["execution_policy_ref", "teams_context_ref"])
@pytest.mark.parametrize("value", ["", 123, False, {}])
def test_governed_enqueue_references_are_nonempty_strings(field, value):
    raw = load_fixture("agent-control-enqueue.json")
    raw["payload"][field] = value
    with pytest.raises(ValidationError):
        AgentControlCommand.model_validate(raw)


def test_teams_payload_extension_preserves_original_command_bytes():
    raw = load_fixture("agent-control-enqueue.json")
    parsed = AgentControlCommand.model_validate(raw)
    assert parsed.payload == raw["payload"]
    assert "teams_context_ref" not in parsed.payload
    raw["payload"].update(
        teams_context_ref="context-example", execution_policy_ref="policy-example"
    )
    assert AgentControlCommand.model_validate(raw).payload == raw["payload"]


def test_resume_requires_target():
    raw = load_fixture("agent-control-resume.json")
    raw["payload"] = {"input": None}
    with pytest.raises(ValidationError):
        AgentControlCommand.model_validate(raw)


def test_resume_target_kind_is_closed_enum():
    raw = load_fixture("agent-control-resume.json")
    raw["payload"]["target"]["kind"] = "snapshot"
    with pytest.raises(ValidationError):
        AgentControlCommand.model_validate(raw)


def test_submit_interaction_requires_interaction_and_token_ref():
    raw = load_fixture("agent-control-submit_interaction.json")
    raw["payload"] = {"run_id": "run-1", "response": "ok"}
    with pytest.raises(ValidationError):
        AgentControlCommand.model_validate(raw)


def test_command_type_is_closed_enum():
    raw = load_fixture("agent-control-enqueue.json")
    raw["command_type"] = "teleport"
    with pytest.raises(ValidationError):
        AgentControlCommand.model_validate(raw)


# ------------------------------------------------------------- receipt 约束


def test_six_receipt_statuses_validate():
    receipts = load_fixture_list("agent-control-receipts.json")
    assert len({r["status"] for r in receipts}) == 6
    for raw in receipts:
        AgentControlReceipt.model_validate(raw)


def test_accepted_receipt_requires_message_id():
    raw = load_fixture("agent-control-receipts.json")[0]
    assert raw["status"] == "accepted"
    raw["message_id"] = None
    with pytest.raises(ValidationError):
        AgentControlReceipt.model_validate(raw)


def test_rejected_receipt_requires_error():
    raw = next(
        record
        for record in load_fixture_list("agent-control-receipts.json")
        if record["status"] == "rejected"
    )
    raw["error"] = None
    with pytest.raises(ValidationError):
        AgentControlReceipt.model_validate(raw)


# --------------------------------------------------------------- 事件约束


def test_runtime_event_envelope_requires_family_version_two():
    raw = load_fixture("session-event-runtime.json") | {"family_version": 1}
    with pytest.raises(ValidationError):
        SessionEventEnvelope.model_validate(raw)


def test_control_event_envelope_requires_family_version_one():
    raw = load_fixture("session-event-control.json")[0] | {"family_version": 2}
    with pytest.raises(ValidationError):
        SessionEventEnvelope.model_validate(raw)


def test_control_event_fixture_covers_initial_event_types():
    event_types = {e["event_type"] for e in load_fixture_list("session-event-control.json")}
    assert "control.command_accepted" in event_types
    assert "control.activation_taken_over" in event_types


# --------------------------------------------------------------- permit 语义


def test_permit_fixtures_cover_valid_expired_tampered():
    permits = load_fixture_list("agent-control-permit.json")
    assert len(permits) == 3
    for raw in permits:
        AgentControlPermit.model_validate(raw)


def test_tampered_permit_signature_does_not_match_claims():
    tampered = next(
        permit
        for permit in load_fixture_list("agent-control-permit.json")
        if permit["permit_id"].endswith("tampered")
    )
    valid = load_fixture_list("agent-control-permit.json")[0]
    assert (
        tampered["claims_digest"] != valid["claims_digest"]
        or tampered["signature"] != valid["signature"]
    )


# ------------------------------------------------------------ lease / capability


def test_lease_fixtures_cover_acquire_renew_takeover():
    leases = load_fixture_list("activation-lease.json")
    assert len(leases) == 3
    takeovers = [lease for lease in leases if lease["activation_id"] != leases[0]["activation_id"]]
    assert takeovers, "takeover 必须换 activation_id"
    fences = [lease["fencing_token"] for lease in leases]
    assert fences == sorted(fences)
    for raw in leases:
        ActivationLease.model_validate(raw)


def test_capability_fixtures_cover_native_and_unavailable():
    matrices = load_fixture_list("runtime-capability.json")
    assert len(matrices) == 2
    modes = set()
    for raw in matrices:
        matrix = RuntimeCapabilityMatrix.model_validate(raw)
        modes.update(
            capability["mode"]
            for capability in matrix.model_dump().values()
            if isinstance(capability, dict) and "mode" in capability
        )
    assert {"native", "unavailable"} <= modes


def test_legacy_capability_fixture_may_omit_execution_modes():
    matrix = RuntimeCapabilityMatrix.model_validate(load_fixture_list("runtime-capability.json")[1])

    assert matrix.goal is None
    assert matrix.loop is None
    assert matrix.plan is None
    assert matrix.interaction_mode is None


def test_legacy_runtime_parser_preserves_additive_execution_modes():
    class LegacyRuntimeCapabilityMatrix(WireModel):
        schema_version: Literal[1] = 1
        cancel: RuntimeCapability
        pause: RuntimeCapability
        resume: RuntimeCapability
        submit_interaction: RuntimeCapability
        attach: RuntimeCapability
        steer: RuntimeCapability
        inject: RuntimeCapability
        checkpoint: RuntimeCapability
        durable_restore: RuntimeCapability

    matrix = LegacyRuntimeCapabilityMatrix.model_validate(
        load_fixture_list("runtime-capability.json")[0]
    )
    round_trip = matrix.model_dump(mode="json")

    assert round_trip["goal"]["mode"] == "native"
    assert round_trip["loop"]["mode"] == "unavailable"
    assert round_trip["loop"]["reason"] == "codex_loop_requires_run_control_spec"
    assert round_trip["plan"]["mode"] == "native"
    assert round_trip["interaction_mode"] == "live_submit"


def test_unsupported_capability_requires_unavailable_mode():
    matrix = RuntimeCapabilityMatrix.model_validate(load_fixture_list("runtime-capability.json")[1])
    assert matrix.pause.mode == "unavailable"
    assert matrix.pause.supported is False
    assert matrix.pause.reason


def test_unsupported_with_native_mode_is_invalid():
    raw = json.loads(json.dumps(load_fixture_list("runtime-capability.json")[1]))
    raw["pause"] = {"supported": False, "mode": "native"}
    with pytest.raises(ValidationError):
        RuntimeCapabilityMatrix.model_validate(raw)


def test_status_snapshot_embeds_capability():
    parsed = AgentStatusSnapshot.model_validate(load_fixture("agent-status-snapshot.json"))
    assert parsed.capability.schema_version == 1


def test_json_value_accepts_recursive_json_and_round_trips():
    payload = {"a": [1, None, {"b": 1.5, "c": [True, "x"]}], "d": "e"}
    raw = load_fixture("agent-control-enqueue.json")
    raw["payload"]["content"] = payload
    command = AgentControlCommand.model_validate(raw)
    assert command.payload["content"] == payload
    assert json.loads(command.model_dump_json())["payload"]["content"] == payload


def test_json_value_rejects_non_json_values():
    for bad in ({1: "non-str key"}, {"bad": object()}, ["nested", {"x": object()}]):
        raw = load_fixture("agent-control-enqueue.json")
        raw["idempotency_key"] = "json-value-reject"
        raw["payload"]["content"] = bad
        with pytest.raises(ValidationError):
            AgentControlCommand.model_validate(raw)
