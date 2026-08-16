"""Envelope-first 未知事件语义:lenient 解析保留信封与 opaque payload。"""

from __future__ import annotations

import pytest

from ksadk.events.canonical import (
    UnknownCanonicalEvent,
    parse_runtime_event,
    parse_runtime_event_lenient,
)


def _envelope(event_type: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "event_id": "evt_lenient_1",
        "seq": 3,
        "timestamp": 1780000000.0,
        "run_id": "run_1",
        "scope_id": "scope_1",
        "source": {"framework": "ksadk"},
        "event_type": event_type,
    }


def test_unknown_event_type_parses_into_opaque_carrier() -> None:
    data = _envelope("future.feature.v3")
    data["custom_field"] = {"nested": [1, 2]}

    event = parse_runtime_event_lenient(data)

    assert isinstance(event, UnknownCanonicalEvent)
    assert event.event_type == "future.feature.v3"
    assert event.event_id == "evt_lenient_1"
    assert event.run_id == "run_1"
    assert event.scope_id == "scope_1"
    assert event.seq == 3
    assert event.payload == {"custom_field": {"nested": [1, 2]}}


def test_lenient_parse_accepts_json_string() -> None:
    import json

    data = json.dumps(_envelope("another.unknown"))

    event = parse_runtime_event_lenient(data)

    assert isinstance(event, UnknownCanonicalEvent)
    assert event.event_type == "another.unknown"


def test_known_event_type_still_validates_strictly() -> None:
    data = _envelope("run.started")
    data["status"] = "running"

    event = parse_runtime_event_lenient(data)

    assert not isinstance(event, UnknownCanonicalEvent)
    assert event.event_type == "run.started"  # type: ignore[union-attr]


def test_strict_parse_rejects_unknown_event_type() -> None:
    with pytest.raises(ValueError):
        parse_runtime_event(_envelope("future.feature.v3"))


def test_broken_envelope_fails_loud_in_lenient_parse() -> None:
    # 信封字段缺失(scope_id)时,lenient 解析也必须拒绝,不得静默降级。
    data = _envelope("future.feature.v3")
    del data["scope_id"]

    with pytest.raises(ValueError):
        parse_runtime_event_lenient(data)


def test_missing_event_type_fails_loud() -> None:
    data = _envelope("run.started")
    del data["event_type"]

    with pytest.raises(ValueError):
        parse_runtime_event_lenient(data)


def test_non_object_input_fails_loud() -> None:
    with pytest.raises(ValueError):
        parse_runtime_event_lenient([1, 2, 3])


def test_known_event_type_with_broken_payload_fails_loud() -> None:
    # 已知 event_type 但缺必填字段(status),不得被包成 opaque,必须严格报错。
    data = _envelope("run.started")  # 缺 status

    with pytest.raises(ValueError):
        parse_runtime_event_lenient(data)
