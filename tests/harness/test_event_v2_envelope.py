"""RuntimeEvent v2 信封测试（长任务方案 §8）。"""

from __future__ import annotations

import pytest

from ksadk.harness.events import (
    SCHEMA_VERSION,
    V2_SCHEMA_VERSION,
    EventType,
    RuntimeEvent,
    project_v2,
)


def _v1_event() -> RuntimeEvent:
    return RuntimeEvent.create(
        EventType.RUN_STARTED,
        agent_id="ar-1",
        user_id="u1",
        session_id="sess-1",
        invocation_id="run-9",
        seq_id=1,
        payload={"status": "running"},
    )


def test_v1_event_stays_v1():
    event = _v1_event()
    assert event.schema_version == SCHEMA_VERSION == 1
    assert event.run_id is None and event.scope_id is None


def test_create_v2_envelope():
    event = RuntimeEvent.create(
        EventType.RUN_STARTED,
        agent_id="ar-1",
        user_id="u1",
        session_id="sess-1",
        invocation_id="run-9",
        seq_id=1,
        payload={"status": "running"},
        run_id="run-9",
        scope_id="agent:ar-1",
    )
    assert event.schema_version == V2_SCHEMA_VERSION == 2
    assert event.run_id == "run-9" and event.scope_id == "agent:ar-1"


def test_to_v2_lossless_upgrade():
    v1 = _v1_event()
    v2 = v1.to_v2()
    assert v2.schema_version == 2
    assert v2.run_id == v1.invocation_id
    assert v2.scope_id == f"agent:{v1.agent_id}"
    # 原事件不变（无损，非原地修改）。
    assert v1.schema_version == 1
    # v2 其余字段与 v1 一致。
    assert v2.event_id == v1.event_id and v2.payload == v1.payload


def test_project_v2_stream():
    events = [_v1_event(), _v1_event()]
    projected = project_v2(events)
    assert all(e.schema_version == 2 and e.run_id and e.scope_id for e in projected)
    # 输入流保持 v1（平台输出边界，不污染内部流）。
    assert all(e.schema_version == 1 for e in events)


def test_project_v2_preserves_explicit_parent_and_does_not_guess_from_colons():
    explicit = RuntimeEvent.create(
        EventType.RUN_STARTED,
        agent_id="tenant:agent",
        user_id="u",
        session_id="s",
        invocation_id="child-run",
        seq_id=1,
        payload={"status": "running"},
        run_id="child-run",
        scope_id="agent:tenant:agent",
        parent_scope_id="agent:explicit-parent",
        parent_run_id="parent-run",
    )
    projected = project_v2([explicit])[0]
    assert projected.parent_scope_id == "agent:explicit-parent"
    assert projected.parent_run_id == "parent-run"


def test_v2_conformance_requires_run_and_scope():
    with pytest.raises(ValueError, match="run_id 与 scope_id"):
        RuntimeEvent.create(
            EventType.RUN_STARTED,
            agent_id="ar-1",
            user_id="u1",
            session_id="sess-1",
            invocation_id="run-9",
            seq_id=1,
            payload={"status": "running"},
            run_id="run-9",  # 缺 scope_id
        )


def test_v1_v2_roundtrip_serialization():
    v1 = _v1_event()
    assert RuntimeEvent.from_json(v1.to_json()) == v1
    v2 = v1.to_v2()
    restored = RuntimeEvent.from_dict(v2.to_dict())
    assert restored.schema_version == 2
    assert restored.run_id == "run-9" and restored.scope_id == "agent:ar-1"
