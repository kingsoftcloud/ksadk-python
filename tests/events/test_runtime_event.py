"""RuntimeEvent v1 schema 测试 (goal-02)。

- conformance fixture(tests/events/fixtures/runtime_event_v1.json)每个事件类型
  一个 canonical 样例:过 JSON Schema + Python 模型 roundtrip + validate_conformance。
- 覆盖性:fixture 覆盖全部 v1 事件类型。
- 负例:未知类型 / 缺 payload 必填键 / 相位滥用 / 错误 schema_version。
- additive:payload 额外键允许。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from ksadk.events.runtime_event import (
    ALL_EVENT_TYPES,
    EventType,
    RuntimeEvent,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "runtime_event_v1.json"
SCHEMA_PATH = (
    Path(__file__).parent.parent.parent
    / "ksadk_runtime_common"
    / "schemas"
    / "runtime_event_v1.json"
)


def _load_fixture() -> tuple[dict, list[dict]]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    base = data["base"]
    merged = [{**base, **event} for event in data["events"]]
    return data, merged


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_fixture_covers_all_v1_event_types():
    _, merged = _load_fixture()
    covered = {event["event_type"] for event in merged}
    assert covered == ALL_EVENT_TYPES


@pytest.mark.parametrize("event", _load_fixture()[1], ids=lambda e: e["event_type"])
def test_fixture_event_validates_against_json_schema(event):
    jsonschema.validate(instance=event, schema=_load_schema())


@pytest.mark.parametrize("event", _load_fixture()[1], ids=lambda e: e["event_type"])
def test_fixture_event_roundtrip_and_conformance(event):
    # dict -> model -> dict
    event_model = RuntimeEvent.from_dict(event)
    event_model.validate_conformance()
    assert RuntimeEvent.from_dict(event_model.to_dict()) == event_model
    # dict -> model -> json -> model
    assert RuntimeEvent.from_json(event_model.to_json()) == event_model
    # 序列化不丢信封字段
    dumped = event_model.to_dict()
    for field in (
        "schema_version",
        "event_id",
        "event_type",
        "timestamp",
        "agent_id",
        "user_id",
        "session_id",
        "invocation_id",
        "seq_id",
    ):
        assert field in dumped
    assert dumped["schema_version"] == 1


def _base_event(**overrides) -> dict:
    event = {
        "schema_version": 1,
        "event_id": "evt_x",
        "event_type": EventType.TEXT_DELTA,
        "timestamp": 1.0,
        "agent_id": "a",
        "user_id": "u",
        "session_id": "s",
        "invocation_id": "i",
        "seq_id": 1,
        "phase": "commentary",
        "payload": {"text": "hi"},
    }
    event.update(overrides)
    return event


def test_create_populates_id_and_timestamp():
    event = RuntimeEvent.create(
        EventType.RUN_STARTED,
        agent_id="a",
        user_id="u",
        session_id="s",
        invocation_id="i",
        seq_id=1,
        payload={"status": "in_progress"},
    )
    assert event.event_id.startswith("evt_")
    assert event.timestamp > 0
    assert event.schema_version == 1


def test_unknown_event_type_rejected():
    with pytest.raises(ValueError, match="unknown event_type"):
        RuntimeEvent.from_dict(_base_event(event_type="text.bogus")).validate_conformance()


def test_deserialize_itself_rejects_unknown_type():
    """from_dict/from_json 反序列化本身就过 conformance(review 修复):
    未知 event_type 不依赖调用方再显式 validate,直接拒绝。"""
    with pytest.raises(ValueError, match="unknown event_type"):
        RuntimeEvent.from_dict(_base_event(event_type="text.bogus"))
    with pytest.raises(ValueError, match="unknown event_type"):
        RuntimeEvent.from_json(
            RuntimeEvent.create(
                "text.delta",
                agent_id="a",
                user_id="u",
                session_id="s",
                invocation_id="i",
                seq_id=1,
                payload={"text": "x"},
            )
            .to_json()
            .replace("text.delta", "text.bogus")
        )


def test_missing_payload_required_key_rejected():
    with pytest.raises(ValueError, match="缺必填键"):
        RuntimeEvent.from_dict(
            _base_event(event_type=EventType.TOOL_CALL_BEGIN, phase=None, payload={"name": "x"})
        ).validate_conformance()


def test_phase_only_allowed_on_text_and_reasoning():
    # tool 事件带 phase -> 拒绝
    with pytest.raises(ValueError, match="phase 仅用于"):
        RuntimeEvent.from_dict(
            _base_event(
                event_type=EventType.TOOL_CALL_BEGIN,
                phase="commentary",
                payload={"call_id": "c", "name": "x"},
            )
        ).validate_conformance()
    # reasoning 带 phase -> 允许
    RuntimeEvent.from_dict(
        _base_event(event_type=EventType.REASONING_DELTA, phase="commentary", payload={"text": "t"})
    ).validate_conformance()


def test_wrong_schema_version_rejected_by_model():
    with pytest.raises(Exception):
        RuntimeEvent.from_dict(_base_event(schema_version=2))


def test_additive_extra_payload_keys_allowed():
    event = RuntimeEvent.from_dict(_base_event(payload={"text": "hi", "future_new_field": 123}))
    event.validate_conformance()  # 不抛
    assert event.payload["future_new_field"] == 123


def test_approval_is_first_class_event_type():
    assert EventType.APPROVAL_REQUESTED in ALL_EVENT_TYPES
    assert EventType.APPROVAL_RESOLVED in ALL_EVENT_TYPES
