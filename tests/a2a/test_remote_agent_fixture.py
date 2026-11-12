"""The SDK owns one immutable, executable cross-language fixture revision."""

from __future__ import annotations

import hashlib
import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from a2a.types import (
    AgentCard,
    Artifact,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import ValidationError

from ksadk.a2a.presentation import (
    A2A_TOOL_ACTIVITY_EXTENSION_URI,
    A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES,
    A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES,
    EXECUTION_SCOPE_SCHEMA,
    PRESENTATION_INPUT_CAPABILITY,
    PRESENTATION_OUTPUT_CAPABILITY,
    ExecutionScopeDescriptor,
    ToolActivity,
    build_public_tool_activity,
    negotiate_presentation_profile,
    validate_scope_transition,
    validate_tool_activity_batch,
)
from ksadk.events.canonical import parse_runtime_event
from ksadk.events.content import TextContent
from ksadk.events.reducer import StreamReducer

FIXTURE = Path(__file__).parent / "fixtures" / "remote_agent" / "v1"
FILES = {
    "adk_two_llm_outputs.jsonl",
    "a2a_stream_tool_terminal.jsonl",
    "a2a_input_continue_cancel.jsonl",
    "expected_runtime_projection.json",
    "expected_conversation_projection.json",
    "expected_v1_projection.json",
}
SCENARIOS = {
    "adk_two_llm_outputs",
    "a2a_stream_tool_terminal",
    "duplicate_delivery",
    "equal_text_distinct_items",
    "out_of_order",
    "root_continues_after_child",
    "parallel_native_call_id_collision",
    "producer_tool_round_trip",
    "closed_snapshot_conflict",
    "disconnect_after_tool_result",
    "input_continue",
    "cancel_matrix",
    "terminal_only_peer",
    "unsafe_tool_payload",
    "mixed_v1_v2",
}


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _canonical_json(value: Any) -> dict[str, Any]:
    return MessageToDict(value, preserving_proto_field_name=False)


def test_manifest_pins_support_matrix_contracts_and_every_file_digest() -> None:
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    assert manifest["fixture_version"] == 1
    assert manifest["schema_revision"] == "a2a-remote-agent/v1"
    assert manifest["a2a_protocol"] == "1.0"
    assert manifest["runtime_event_schema"] == 2
    assert manifest["owner"] == "ksadk-python"
    assert manifest["owner_base_sha"] == "bae8c886e87ea2f62f440945dd8aa591759cea5d"
    assert manifest["profiles"] == ["flat-v1", "agent-block-v1"]
    assert manifest["capabilities"] == {
        "input": PRESENTATION_INPUT_CAPABILITY,
        "output": PRESENTATION_OUTPUT_CAPABILITY,
        "tool_activity_extension": A2A_TOOL_ACTIVITY_EXTENSION_URI,
    }
    assert manifest["limits"] == {
        "tool_activity_event_bytes": A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES,
        "tool_activity_invocation_bytes": A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES,
    }
    assert set(manifest["scenarios"]) == SCENARIOS
    assert set(manifest["sha256"]) == FILES
    for filename, expected in manifest["sha256"].items():
        assert hashlib.sha256((FIXTURE / filename).read_bytes()).hexdigest() == expected


def test_frozen_adk_contract_is_executable_in_the_requested_environment() -> None:
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    support = manifest["support_matrix"]["google_adk"]
    assert support["remote_agent_minimum"] == "2.5.0"
    assert support["contract_tested"] == ["2.5.0"]
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    installed = version("google-adk")
    expected = os.environ.get("KSADK_TEST_EXPECTED_ADK_VERSION")
    if expected is not None:
        assert installed == expected
    # The manifest freezes the original 2.5 contract evidence, not every CI
    # interpreter. Execute the same protobuf/API seam in the SDK ADK matrix.
    assert installed in SpecifierSet(">=1.34.0,<3.0.0")
    if Version(installed) < Version(support["remote_agent_minimum"]):
        pytest.skip("upstream RemoteA2aAgent requires ADK >= 2.5 with A2A 1.1; SDK adapters have separate coverage")
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

    records = _lines(FIXTURE / "a2a_stream_tool_terminal.jsonl")
    card_payload = next(item["payload"] for item in records if item["kind"] == "agent_card")
    remote = RemoteA2aAgent(
        name="fixture_remote_agent", agent_card=ParseDict(card_payload, AgentCard()),
    )
    assert remote.name == "fixture_remote_agent"


def test_fixture_records_cover_matrix_and_round_trip_pinned_a2a_protobufs() -> None:
    records = [
        record
        for filename in (
            "adk_two_llm_outputs.jsonl",
            "a2a_stream_tool_terminal.jsonl",
            "a2a_input_continue_cancel.jsonl",
        )
        for record in _lines(FIXTURE / filename)
    ]
    assert {record["scenario"] for record in records} == SCENARIOS
    proto_types = {
        "agent_card": AgentCard,
        "message": Message,
        "task": Task,
        "artifact": Artifact,
        "status_update": TaskStatusUpdateEvent,
        "artifact_update": TaskArtifactUpdateEvent,
    }
    seen: set[str] = set()
    for record in records:
        kind = record["kind"]
        if kind == "runtime_event":
            event = parse_runtime_event(record["payload"])
            assert event.run_id == record["payload"]["run_id"]
        elif kind == "scope_descriptor":
            assert ExecutionScopeDescriptor.model_validate(record["payload"]).schema_ == (
                EXECUTION_SCOPE_SCHEMA
            )
        elif kind == "tool_activity":
            ToolActivity.model_validate(record["payload"])
        elif kind in proto_types:
            parsed = ParseDict(record["payload"], proto_types[kind]())
            assert _canonical_json(parsed) == record["payload"]
            seen.add(kind)
    assert seen == set(proto_types)


@pytest.mark.parametrize("replay_count", [1, 10, 100])
def test_normative_runtime_stream_is_executable_by_the_strict_reducer(replay_count) -> None:
    reducer = StreamReducer()
    events = [
        parse_runtime_event(record["payload"])
        for record in _lines(FIXTURE / "a2a_stream_tool_terminal.jsonl")
        if record["scenario"] == "a2a_stream_tool_terminal"
        and record["kind"] == "runtime_event"
    ]
    for event in events:
        reducer.apply(event)
    snapshot = reducer.snapshot()
    assert snapshot.status == "completed"
    assert snapshot.last_seq == 22
    assert len(snapshot.items) == 7
    assert [
        part.text
        for item in snapshot.items
        for part in item.parts
        if isinstance(part, TextContent)
    ] == ["first answer", "second answer", "Root final answer"]
    for _ in range(replay_count):
        for event in events:
            assert reducer.apply(event).applied is False
        assert reducer.snapshot() == snapshot


def test_scope_descriptor_is_public_immutable_and_transition_checked() -> None:
    records = _lines(FIXTURE / "a2a_stream_tool_terminal.jsonl")
    descriptors = [
        ExecutionScopeDescriptor.model_validate(record["payload"])
        for record in records
        if record["kind"] == "scope_descriptor"
        and record["scenario"] == "a2a_stream_tool_terminal"
    ]
    assert [value.status for value in descriptors] == [
        "submitted",
        "working",
        "completed",
    ]
    for previous, current in zip(descriptors[:-1], descriptors[1:], strict=True):
        validate_scope_transition(previous, current)
    assert descriptors[0].public_locator() == descriptors[-1].public_locator()
    assert not {
        "remote_task_id",
        "remote_context_id",
        "endpoint",
        "credential",
        "permit",
    }.intersection(descriptors[-1].model_dump())
    conflicting = descriptors[-1].model_copy(update={"binding_id": "changed"})
    with pytest.raises(ValueError, match="SCOPE_LOCATOR_CONFLICT"):
        validate_scope_transition(descriptors[0], conflicting)


def test_tool_activity_policy_redacts_then_enforces_both_size_limits() -> None:
    activity = build_public_tool_activity(
        {
            "schema": "ksadk.a2a.tool-activity/v1",
            "event_id": "remote-occurrence-17",
            "item_id": "remote-call-item-17",
            "call_id": "call_1",
            "item_kind": "tool_result",
            "operation": "completed",
            "execution_status": "completed",
            "name": "query_metrics",
            "arguments": None,
            "result": {
                "header": "Bearer secret",
                "url": "http://127.0.0.1/admin",
                "traceback": "Traceback: private stack",
                "long": "x" * 20_000,
            },
            "is_error": False,
            "visibility": "public",
            "truncated": False,
        }
    )
    wire = activity.model_dump_json()
    assert activity.truncated is True
    assert "secret" not in wire
    assert "127.0.0.1" not in wire
    assert "private stack" not in wire
    assert len(wire.encode()) <= A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES
    validate_tool_activity_batch([activity])
    with pytest.raises(ValueError, match="INVOCATION_LIMIT"):
        validate_tool_activity_batch(
            [activity] * (A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES // len(wire.encode()) + 2)
        )
    with pytest.raises(ValidationError, match="unsafe public tool activity"):
        ToolActivity.model_validate(
            {**activity.model_dump(), "arguments": {"token": "raw-secret"}}
        )


def test_scope_descriptor_rejects_parent_cycles_and_trigger_mismatch() -> None:
    record = next(
        record
        for record in _lines(FIXTURE / "a2a_stream_tool_terminal.jsonl")
        if record["kind"] == "scope_descriptor"
    )
    payload = record["payload"]
    with pytest.raises(ValidationError, match="PARENT_CYCLE"):
        ExecutionScopeDescriptor.model_validate(
            {**payload, "parent_scope_id": payload["scope_id"]}
        )
    with pytest.raises(ValidationError, match="TRIGGER_PARENT_MISMATCH"):
        ExecutionScopeDescriptor.model_validate(
            {
                **payload,
                "trigger_ref": {**payload["trigger_ref"], "scope_id": "other-root"},
            }
        )


@pytest.mark.parametrize(
    ("producer", "requested", "renderer", "expected"),
    [
        (False, None, True, "flat-v1"),
        (False, "agent-block-v1", True, "flat-v1"),
        (True, None, True, "flat-v1"),
        (True, "agent-block-v1", False, "flat-v1"),
        (True, "agent-block-v1", True, "agent-block-v1"),
        (True, "flat-v1", True, "flat-v1"),
    ],
)
def test_profile_negotiation_is_an_explicit_three_way_intersection(
    producer: bool, requested: str | None, renderer: bool, expected: str
) -> None:
    assert (
        negotiate_presentation_profile(
            producer_declares_capability=producer,
            requested_profile=requested,
            renderer_supports_agent_block=renderer,
        )
        == expected
    )
    with pytest.raises(ValueError, match="unsupported presentation profile"):
        negotiate_presentation_profile(
            producer_declares_capability=True,
            requested_profile="custom-profile",
            renderer_supports_agent_block=True,
        )


def test_expected_projections_freeze_authoritative_and_legacy_lanes() -> None:
    runtime = json.loads((FIXTURE / "expected_runtime_projection.json").read_text())
    conversation = json.loads(
        (FIXTURE / "expected_conversation_projection.json").read_text()
    )
    legacy = json.loads((FIXTURE / "expected_v1_projection.json").read_text())
    assert runtime["root_status"] == "completed"
    assert runtime["root_output_refs"] == ["root-message-2"]
    assert runtime["scopes"]["child-scope-1"]["status"] == "completed"
    assert runtime["scopes"]["child-scope-1"]["messages"] == [
        "first answer",
        "second answer",
    ]
    assert conversation["profile"] == "agent-block-v1"
    assert conversation["items"][0]["kind"] == "agent"
    assert conversation["items"][0]["tool_blocks"] == [
        {"call_id": "remote-tool-17", "execution_status": "completed"}
    ]
    assert legacy == {
        "profile": "flat-v1",
        "items": [
            {
                "kind": "assistant_text",
                "item_id": "root-message-2",
                "text": "Root final answer",
            }
        ],
    }
