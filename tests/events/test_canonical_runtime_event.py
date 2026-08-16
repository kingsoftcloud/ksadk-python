"""Contract tests for the canonical RuntimeEvent schema."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import get_args

import jsonschema
import pytest
from pydantic import ValidationError

from ksadk.events.canonical import (
    ALL_EVENT_TYPES,
    ContinuationKind,
    Framework,
    InteractionKind,
    ItemKind,
    RuntimeEvent,
    SourceProtocol,
    dump_runtime_event,
    parse_runtime_event,
)
from ksadk.events.content import ContentValue

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "runtime_event_v2.json"
SCHEMA_PATH = (
    Path(__file__).parent.parent.parent
    / "ksadk_runtime_common"
    / "schemas"
    / "runtime_event_v2.json"
)


def _fixtures() -> list[dict]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return [{**document["base"], **event} for event in document["events"]]


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_fixture_covers_every_canonical_event_family() -> None:
    fixtures = _fixtures()
    assert {event["event_type"] for event in fixtures} == ALL_EVENT_TYPES
    assert {part["content_type"] for event in fixtures for part in _parts(event)} == {
        "artifact",
        "data",
        "json",
        "text",
        "tool_call",
        "tool_result",
    }


@pytest.mark.parametrize("event", _fixtures(), ids=lambda event: event["event_type"])
def test_literal_fixture_validates_with_pydantic_and_json_schema(event: dict) -> None:
    jsonschema.validate(instance=event, schema=_schema())
    parsed = parse_runtime_event(event)
    assert parsed.event_type == event["event_type"]
    assert dump_runtime_event(parsed) == event
    assert parse_runtime_event(json.dumps(event)) == parsed


def test_runtime_event_is_a_discriminated_union() -> None:
    event = _fixtures()[0]
    event["event_type"] = "run.unknown"
    with pytest.raises(ValidationError):
        parse_runtime_event(event)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=event, schema=_schema())


def test_schema_version_two_is_required() -> None:
    event = _fixtures()[0]
    event["schema_version"] = 1
    with pytest.raises(ValidationError):
        parse_runtime_event(event)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=event, schema=_schema())


def test_source_protocol_is_a_typed_a2ui_discriminator_in_both_schemas() -> None:
    event = next(
        copy.deepcopy(event) for event in _fixtures() if event["event_type"] == "item.started"
    )
    event["source"]["protocol"] = "a2ui"

    parsed = parse_runtime_event(event)

    assert parsed.source.protocol == "a2ui"
    assert dump_runtime_event(parsed) == event
    jsonschema.validate(instance=event, schema=_schema())


def test_source_protocol_rejects_unknown_values_in_both_schemas() -> None:
    event = copy.deepcopy(_fixtures()[0])
    event["source"]["protocol"] = "unknown"

    with pytest.raises(ValidationError):
        parse_runtime_event(event)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=event, schema=_schema())


def test_completed_item_has_an_authoritative_snapshot() -> None:
    event = next(event for event in _fixtures() if event["event_type"] == "item.completed")
    parsed = parse_runtime_event(event)
    assert parsed.snapshot.parts[0].part_id == "text-0"  # type: ignore[union-attr]


def test_run_completed_output_refs_preserve_declared_order() -> None:
    event = next(event for event in _fixtures() if event["event_type"] == "run.completed")
    parsed = parse_runtime_event(event)
    assert [ref.item_id for ref in parsed.output_refs] == ["item-message", "item-data"]  # type: ignore[union-attr]


def _invalid_documents() -> list[tuple[str, dict]]:
    fixtures = _fixtures()
    run_started = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "run.started"
    )
    item_started = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "item.started"
    )
    item_updated = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "item.updated"
    )
    run_progress = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "run.progress"
    )
    continuation_created = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "continuation.created"
    )
    interaction_requested = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "interaction.requested"
    )
    interaction_resolved = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "interaction.resolved"
    )

    wrong_status = copy.deepcopy(run_started)
    wrong_status["status"] = "completed"

    leaked_item_fields = copy.deepcopy(item_started)
    leaked_item_fields["op"] = "append"
    leaked_item_fields["update"] = {
        "content_type": "text",
        "part_id": "text-0",
        "text": "leaked",
    }

    missing_schema_version = copy.deepcopy(run_started)
    del missing_schema_version["schema_version"]

    missing_status = copy.deepcopy(run_started)
    del missing_status["status"]

    coerced_seq = copy.deepcopy(run_started)
    coerced_seq["seq"] = "1"

    coerced_timestamp = copy.deepcopy(run_started)
    coerced_timestamp["timestamp"] = "1723334400.0"

    coerced_progress = copy.deepcopy(run_progress)
    coerced_progress["progress"] = "0.5"

    coerced_boolean = copy.deepcopy(continuation_created)
    coerced_boolean["resumable"] = 1

    empty_part_id = copy.deepcopy(item_updated)
    empty_part_id["update"]["part_id"] = ""

    empty_call_id = copy.deepcopy(item_started)
    tool_call = next(
        part for part in empty_call_id["initial"]["parts"] if part["content_type"] == "tool_call"
    )
    tool_call["call_id"] = ""

    coerced_tool_boolean = copy.deepcopy(item_started)
    tool_result = next(
        part
        for part in coerced_tool_boolean["initial"]["parts"]
        if part["content_type"] == "tool_result"
    )
    tool_result["is_error"] = 0

    mismatched_request_kind = copy.deepcopy(interaction_requested)
    mismatched_request_kind["interaction_kind"] = "structured_input"

    mismatched_response_kind = copy.deepcopy(interaction_resolved)
    mismatched_response_kind["interaction_kind"] = "structured_input"

    return [
        ("wrong_run_status", wrong_status),
        ("leaked_item_fields", leaked_item_fields),
        ("missing_schema_version", missing_schema_version),
        ("missing_run_status", missing_status),
        ("coerced_seq", coerced_seq),
        ("coerced_timestamp", coerced_timestamp),
        ("coerced_progress", coerced_progress),
        ("coerced_boolean", coerced_boolean),
        ("coerced_tool_boolean", coerced_tool_boolean),
        ("empty_part_id", empty_part_id),
        ("empty_call_id", empty_call_id),
        ("mismatched_request_kind", mismatched_request_kind),
        ("mismatched_response_kind", mismatched_response_kind),
    ]


_INVALID_DOCUMENTS = _invalid_documents()


@pytest.mark.parametrize(
    ("case", "event"),
    _INVALID_DOCUMENTS,
    ids=[case for case, _event in _INVALID_DOCUMENTS],
)
def test_pydantic_and_json_schema_reject_the_same_invalid_documents(case: str, event: dict) -> None:
    del case
    with pytest.raises(ValidationError):
        parse_runtime_event(event)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=event, schema=_schema())


def test_checked_in_schema_event_list_matches_the_runtime_union() -> None:
    schema_event_types = {
        branch["properties"]["event_type"]["const"] for branch in _schema()["oneOf"]
    }
    assert schema_event_types == ALL_EVENT_TYPES


def test_checked_in_schema_and_runtime_require_the_same_event_fields() -> None:
    schema = _schema()
    common_required = set(schema["required"])
    branches = {branch["properties"]["event_type"]["const"]: branch for branch in schema["oneOf"]}
    for event in _fixtures():
        required = common_required | set(branches[event["event_type"]]["required"])
        for field in required:
            invalid = copy.deepcopy(event)
            del invalid[field]
            with pytest.raises(ValidationError):
                parse_runtime_event(invalid)
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(instance=invalid, schema=schema)


def test_checked_in_schema_and_runtime_isolate_every_event_branch() -> None:
    schema = _schema()
    common_fields = set(schema["properties"])
    branches = {branch["properties"]["event_type"]["const"]: branch for branch in schema["oneOf"]}
    fixtures_by_type = {event["event_type"]: event for event in _fixtures()}
    example_values = {
        key: copy.deepcopy(value)
        for event in fixtures_by_type.values()
        for key, value in event.items()
        if key not in common_fields
    }

    for event_type, event in fixtures_by_type.items():
        allowed = common_fields | set(branches[event_type]["properties"])
        for field, value in example_values.items():
            if field in allowed:
                continue
            invalid = copy.deepcopy(event)
            invalid[field] = copy.deepcopy(value)
            with pytest.raises(ValidationError):
                parse_runtime_event(invalid)
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(instance=invalid, schema=schema)


def test_optional_nulls_have_the_same_boundary_semantics() -> None:
    fixtures = _fixtures()
    documents = []

    progress = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "run.progress"
    )
    progress.update({"run_seq": None, "parent_scope_id": None, "progress": None, "message": None})
    progress["source"].update(
        {
            "native_event_id": None,
            "native_cursor": None,
            "native_run_id": None,
            "native_item_id": None,
        }
    )
    documents.append(progress)

    item_started = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "item.started"
    )
    item_started.update({"phase": None, "initial": None})
    documents.append(item_started)

    failed = next(copy.deepcopy(event) for event in fixtures if event["event_type"] == "run.failed")
    failed["error"].update({"message": None, "item_id": None, "source_ref": None})
    documents.append(failed)

    completed = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "run.completed"
    )
    completed["output_refs"][0]["part_id"] = None
    documents.append(completed)

    for document in documents:
        parse_runtime_event(document)
        jsonschema.validate(instance=document, schema=_schema())


def _nested_contract_invalid_documents() -> list[tuple[str, dict]]:
    fixtures = _fixtures()
    completed = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "item.completed"
    )
    completed["snapshot"] = {}

    started = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "item.started"
    )
    started["initial"] = {}

    approval = next(
        copy.deepcopy(event)
        for event in fixtures
        if event["event_type"] == "interaction.requested"
        and event["interaction_kind"] == "approval"
    )
    approval["request"]["kind"] = ""
    return [
        ("completed_snapshot_missing_parts", completed),
        ("started_initial_missing_parts", started),
        ("approval_kind_empty", approval),
    ]


_NESTED_CONTRACT_INVALID_DOCUMENTS = _nested_contract_invalid_documents()


@pytest.mark.parametrize(
    ("case", "event"),
    _NESTED_CONTRACT_INVALID_DOCUMENTS,
    ids=[case for case, _event in _NESTED_CONTRACT_INVALID_DOCUMENTS],
)
def test_nested_contract_is_rejected_by_both_boundaries(case: str, event: dict) -> None:
    del case
    with pytest.raises(ValidationError):
        parse_runtime_event(event)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=event, schema=_schema())


def _integral_number_documents() -> list[tuple[str, dict]]:
    fixtures = _fixtures()
    started = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "run.started"
    )
    started.update({"seq": 1.0, "run_seq": 1.0})

    compacted = next(
        copy.deepcopy(event)
        for event in fixtures
        if event["event_type"] == "context.compaction.completed"
    )
    compacted["compacted_until_seq"] = 10.0

    usage = next(
        copy.deepcopy(event) for event in fixtures if event["event_type"] == "usage.reported"
    )
    for field in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
    ):
        usage[field] = float(usage[field])
    return [("seq_and_run_seq", started), ("compacted_seq", compacted), ("usage", usage)]


@pytest.mark.parametrize(
    ("case", "event"),
    _integral_number_documents(),
    ids=["seq_and_run_seq", "compacted_seq", "usage"],
)
def test_mathematically_integral_json_numbers_are_accepted(case: str, event: dict) -> None:
    del case
    parse_runtime_event(event)
    jsonschema.validate(instance=event, schema=_schema())


def _non_integer_documents() -> list[tuple[str, dict]]:
    fixtures = _fixtures()
    locations = [
        ("seq", "run.started", "seq"),
        ("run_seq", "run.started", "run_seq"),
        ("compacted_seq", "context.compaction.completed", "compacted_until_seq"),
        ("usage_token", "usage.reported", "total_tokens"),
    ]
    documents = []
    for label, event_type, field in locations:
        base = next(copy.deepcopy(event) for event in fixtures if event["event_type"] == event_type)
        for value_label, value in (("bool", True), ("string", "1"), ("fraction", 1.5)):
            event = copy.deepcopy(base)
            event[field] = value
            documents.append((f"{label}_{value_label}", event))
    return documents


_NON_INTEGER_DOCUMENTS = _non_integer_documents()


@pytest.mark.parametrize(
    ("case", "event"),
    _NON_INTEGER_DOCUMENTS,
    ids=[case for case, _event in _NON_INTEGER_DOCUMENTS],
)
def test_non_integer_json_values_are_rejected_by_both_boundaries(case: str, event: dict) -> None:
    del case
    with pytest.raises(ValidationError):
        parse_runtime_event(event)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=event, schema=_schema())


def _discriminated_literal_values(union: object, field: str) -> set[str]:
    models = get_args(get_args(union)[0])
    return {get_args(model.model_fields[field].annotation)[0] for model in models}


def test_checked_in_schema_literals_match_code_unions() -> None:
    schema = _schema()
    content_defs = {
        reference["$ref"].rsplit("/", 1)[-1]
        for reference in schema["$defs"]["contentValue"]["oneOf"]
    }
    schema_content_types = {
        schema["$defs"][definition]["properties"]["content_type"]["const"]
        for definition in content_defs
    }
    assert schema_content_types == _discriminated_literal_values(ContentValue, "content_type")
    assert set(schema["$defs"]["sourceRef"]["properties"]["framework"]["enum"]) == set(
        get_args(Framework)
    )
    assert set(schema["$defs"]["sourceRef"]["properties"]["protocol"]["enum"]) == {
        *get_args(SourceProtocol),
        None,
    }
    assert set(schema["$defs"]["itemKind"]["enum"]) == set(get_args(ItemKind))
    assert set(schema["$defs"]["interactionKind"]["enum"]) == set(get_args(InteractionKind))
    assert set(schema["$defs"]["continuationKind"]["enum"]) == set(get_args(ContinuationKind))


def _parts(event: dict) -> list[dict]:
    if "snapshot" in event:
        return event["snapshot"]["parts"]
    if "initial" in event and event["initial"] is not None:
        return event["initial"]["parts"]
    if "update" in event:
        return [event["update"]]
    return []


def _runtime_event_type_check(_: RuntimeEvent) -> None:
    """Make RuntimeEvent part of the import contract without changing runtime behavior."""
