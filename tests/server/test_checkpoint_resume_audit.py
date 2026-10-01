from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ksadk.events.canonical import ContinuationCreated, ContinuationResumed, SourceRef
from ksadk.events.canonical_store import RuntimeEventStore, runtime_event_to_session_event
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.routes import dependencies
from ksadk.server.routes.models import ListSessionCheckpointsActionRequest
from ksadk.server.routes.projection import (
    _apply_checkpoint_resume_audit,
    _checkpoint_event_to_action_payload,
    _record_resume_audit,
)
from ksadk.server.routes.sessions import _list_checkpoints_payload
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService

pytest_plugins = ("tests.kernel.teams_host_harness",)


def _continuation_created() -> ContinuationCreated:
    return ContinuationCreated(
        schema_version=2,
        event_id="event-checkpoint-created",
        seq=1,
        timestamp=100.0,
        run_id="run-1",
        run_seq=1,
        scope_id="run:run-1",
        source=SourceRef(
            framework="langgraph",
            metadata={"backend": "postgres", "scope": "thread", "durable": True},
        ),
        continuation_id="checkpoint-1",
        continuation_kind="graph_checkpoint",
        resumable=True,
        ref={"checkpoint_id": "checkpoint-1", "checkpoint_ns": ""},
    )


def _continuation_resumed() -> ContinuationResumed:
    return ContinuationResumed(
        schema_version=2,
        event_id="event-checkpoint-resumed",
        seq=2,
        timestamp=101.0,
        run_id="run-1",
        run_seq=2,
        scope_id="run:run-1",
        source=SourceRef(framework="langgraph"),
        continuation_id="checkpoint-1",
        continuation_kind="graph_checkpoint",
        resume_attempt_id="resume-attempt-1",
    )


def test_canonical_continuation_resumed_updates_checkpoint_audit() -> None:
    created = runtime_event_to_session_event("session-1", _continuation_created())
    resumed = runtime_event_to_session_event("session-1", _continuation_resumed())
    audit: dict[str, dict[tuple[str, str], dict[str, object]]] = {}

    _record_resume_audit(audit, resumed)

    checkpoint = _checkpoint_event_to_action_payload(created)
    assert checkpoint is not None
    checkpoint = _apply_checkpoint_resume_audit(checkpoint, audit["session-1"])

    assert checkpoint["ResumeCount"] == 1
    assert checkpoint["LastResumedAt"] == 101.0
    assert checkpoint["CheckpointStatus"] == "resumed"
    assert checkpoint["Durable"] is True


@pytest.mark.asyncio
async def test_typed_envelope_checkpoint_projection_uses_envelope_identity() -> None:
    """Typed runtime envelopes keep producer and carrier event ids distinct."""

    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(
        SessionServiceEventStore(service),
        session_id="session-1",
    )
    guard = ActivationWriteGuard(activation_id="activation-1", fencing_token=1)
    await store.append(_continuation_created(), guard=guard)
    await store.append(_continuation_resumed(), guard=guard)

    request = ListSessionCheckpointsActionRequest(
        AgentId="agent-1", SessionId="session-1", Limit=10
    )
    with dependencies.bind_session_service(service):
        payload = await _list_checkpoints_payload(request, PlatformIdentityContext())

    assert payload["Total"] == 1
    checkpoint = payload["Checkpoints"][0]
    assert checkpoint["CheckpointId"] == "checkpoint-1"
    assert checkpoint["ResumeCount"] == 1
    assert checkpoint["LastResumedAt"] == 101.0


def test_resume_audit_uses_monotonic_timestamp_and_skips_unknown_events() -> None:
    resumed = runtime_event_to_session_event(
        "session-1", _continuation_resumed().model_copy(update={"timestamp": 101.0})
    )
    older = runtime_event_to_session_event(
        "session-1",
        _continuation_resumed().model_copy(
            update={"event_id": "event-checkpoint-resumed-older", "timestamp": 99.0}
        ),
    )
    unknown = SessionEvent(
        id="future-event",
        session_id="session-1",
        event_type="continuation.future",
        content={
            "runtime_event": {
                "schema_version": 2,
                "event_id": "future-event",
                "event_type": "continuation.future",
                "seq": 3,
                "timestamp": 102.0,
                "run_id": "run-1",
                "scope_id": "run:run-1",
                "source": {"framework": "langgraph"},
                "continuation_id": "checkpoint-1",
            }
        },
        metadata={"ksadk_canonical_runtime_event": True, "schema_version": 2},
        seq_id=3,
    )
    audit: dict[str, dict[tuple[str, str], dict[str, object]]] = {}

    _record_resume_audit(audit, resumed)
    _record_resume_audit(audit, older)
    _record_resume_audit(audit, unknown)

    assert _checkpoint_event_to_action_payload(unknown) is None
    assert audit["session-1"][("run-1", "checkpoint-1")] == {
        "resume_count": 2,
        "last_resumed_at": 101.0,
    }


def test_resume_audit_ignores_nan_and_accepts_numeric_string_timestamp() -> None:
    nan_event = runtime_event_to_session_event(
        "session-1", _continuation_resumed().model_copy(update={"event_id": "nan"})
    )
    nan_event.content["runtime_event"]["timestamp"] = float("nan")
    nan_event.timestamp = 50.0
    numeric_event = runtime_event_to_session_event(
        "session-1", _continuation_resumed().model_copy(update={"event_id": "numeric"})
    )
    numeric_event.content["runtime_event"]["timestamp"] = "102.5"
    numeric_event.timestamp = 50.0
    audit: dict[str, dict[tuple[str, str], dict[str, object]]] = {}

    _record_resume_audit(audit, nan_event)
    _record_resume_audit(audit, numeric_event)

    assert audit["session-1"][("run-1", "checkpoint-1")] == {
        "resume_count": 2,
        "last_resumed_at": 102.5,
    }


def test_malformed_known_resume_event_remains_fail_loud() -> None:
    malformed = SessionEvent(
        id="malformed-resume",
        session_id="session-1",
        event_type="continuation.resumed",
        content={
            "runtime_event": {
                "schema_version": 2,
                "event_id": "malformed-resume",
                "event_type": "continuation.resumed",
                "seq": 4,
                "timestamp": 103.0,
                "run_id": "run-1",
                "scope_id": "run:run-1",
                "source": {"framework": "langgraph"},
                "continuation_id": "checkpoint-1",
                "continuation_kind": "graph_checkpoint",
            }
        },
        metadata={"ksadk_canonical_runtime_event": True, "schema_version": 2},
        seq_id=4,
    )

    with pytest.raises(ValidationError):
        _checkpoint_event_to_action_payload(malformed)


def test_known_canonical_carrier_mismatch_fails_loud() -> None:
    mismatched = runtime_event_to_session_event("session-1", _continuation_resumed())
    mismatched.id = "wrong-storage-id"
    audit: dict[str, dict[tuple[str, str], dict[str, object]]] = {}

    with pytest.raises(ValueError, match="storage id"):
        _record_resume_audit(audit, mismatched)
    with pytest.raises(ValueError, match="storage id"):
        _checkpoint_event_to_action_payload(mismatched)

    mismatched_metadata = runtime_event_to_session_event(
        "session-1", _continuation_resumed().model_copy(update={"event_id": "metadata"})
    )
    mismatched_metadata.metadata["canonical_event_id"] = "wrong-event-id"
    with pytest.raises(ValueError, match="event id metadata"):
        _record_resume_audit(audit, mismatched_metadata)
    with pytest.raises(ValueError, match="event id metadata"):
        _checkpoint_event_to_action_payload(mismatched_metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_checkpoint_stats_count_canonical_resume_facts(tmp_path, backend: str) -> None:
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(tmp_path / "sessions.sqlite")
    )
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    await store.append(
        "session-1",
        [
            _continuation_created(),
            _continuation_resumed(),
            _continuation_resumed(),  # same event id: idempotent replay
        ],
    )

    stats = await service.get_checkpoint_stats([("session-1", "run-1", "checkpoint-1")])
    audit = stats["audits"][("session-1", "run-1", "checkpoint-1")]

    assert audit == {"resume_count": 1, "last_resumed_at": 101.0}
    assert stats["latest_seq_ids"][("session-1", "run-1")] == 1
    lookup = await service.get_checkpoint_lookup_stats(
        "session-1", "run-1", "checkpoint-1"
    )
    assert lookup["candidate"] is not None
    assert lookup["candidate"].event_type == "continuation.created"
    assert lookup["candidate"].seq_id == 1
    assert lookup["max_seq_id"] == 1

    await store.append(
        "session-1",
        [
            _continuation_resumed().model_copy(
                update={"event_id": "event-checkpoint-resumed-older", "timestamp": 99.0}
            )
        ],
    )
    stats = await service.get_checkpoint_stats([("session-1", "run-1", "checkpoint-1")])
    audit = stats["audits"][("session-1", "run-1", "checkpoint-1")]
    assert audit == {"resume_count": 2, "last_resumed_at": 101.0}

    mismatched = runtime_event_to_session_event(
        "session-1",
        _continuation_resumed().model_copy(
            update={"event_id": "event-checkpoint-resumed-mismatch", "timestamp": 102.0}
        ),
    )
    mismatched.timestamp = 50.0  # carrier timestamp must not override canonical v2 time
    await service.append_event("session-1", mismatched)
    stats = await service.get_checkpoint_stats([("session-1", "run-1", "checkpoint-1")])
    audit = stats["audits"][("session-1", "run-1", "checkpoint-1")]
    assert audit == {"resume_count": 3, "last_resumed_at": 102.0}


@pytest.mark.asyncio
async def test_checkpoint_listing_rest_projects_canonical_audit_and_skips_unknown() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    created = _continuation_created()
    resumed = _continuation_resumed()
    await store.append("session-1", [created, resumed])
    await service.append_event(
        "session-1",
        SessionEvent(
            id="future-event",
            session_id="session-1",
            event_type="continuation.future",
            content={
                "runtime_event": {
                    "schema_version": 2,
                    "event_id": "future-event",
                    "event_type": "continuation.future",
                    "seq": 3,
                    "timestamp": 102.0,
                    "run_id": "run-1",
                    "scope_id": "run:run-1",
                    "source": {"framework": "langgraph"},
                }
            },
            metadata={"ksadk_canonical_runtime_event": True, "schema_version": 2},
        ),
    )

    request = ListSessionCheckpointsActionRequest(
        AgentId="agent-1", SessionId="session-1", Limit=10
    )
    with dependencies.bind_session_service(service):
        payload = await _list_checkpoints_payload(request, PlatformIdentityContext())

    assert payload["Total"] == 1
    checkpoint = payload["Checkpoints"][0]
    assert checkpoint["CheckpointId"] == "checkpoint-1"
    assert checkpoint["ResumeCount"] == 1
    assert checkpoint["LastResumedAt"] == 101.0
    assert checkpoint["Durable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_checkpoint_stats_ignores_malformed_canonical_timestamp(
    tmp_path, backend: str
) -> None:
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(tmp_path / "sessions.sqlite")
    )
    await service.create_session("agent-1", "user-1", session_id="session-1")
    malformed = runtime_event_to_session_event(
        "session-1", _continuation_resumed().model_copy(update={"event_id": "malformed"})
    )
    malformed.content["runtime_event"]["timestamp"] = "1e999999999999999999999"
    malformed.timestamp = 50.0
    await service.append_event("session-1", malformed)
    numeric = runtime_event_to_session_event(
        "session-1", _continuation_resumed().model_copy(update={"event_id": "numeric"})
    )
    numeric.content["runtime_event"]["timestamp"] = "102.5"
    numeric.timestamp = 50.0
    await service.append_event("session-1", numeric)
    stale_carrier = runtime_event_to_session_event(
        "session-1", _continuation_resumed().model_copy(update={"event_id": "stale-carrier"})
    )
    stale_carrier.content["runtime_event"]["timestamp"] = 101.0
    stale_carrier.timestamp = 1000.0
    await service.append_event("session-1", stale_carrier)

    stats = await service.get_checkpoint_stats([("session-1", "run-1", "checkpoint-1")])
    assert stats["audits"][("session-1", "run-1", "checkpoint-1")] == {
        "resume_count": 3,
        "last_resumed_at": 102.5,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_checkpoint_stats_rejects_malformed_known_resume(
    tmp_path, backend: str
) -> None:
    """Stats must fail loud like projection for broken known canonical facts."""

    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(tmp_path / "sessions.sqlite")
    )
    await service.create_session("agent-1", "user-1", session_id="session-1")
    malformed = runtime_event_to_session_event(
        "session-1", _continuation_resumed().model_copy(update={"event_id": "malformed"})
    )
    malformed.content["runtime_event"].pop("resume_attempt_id")
    await service.append_event("session-1", malformed)

    with pytest.raises(ValidationError):
        await service.get_checkpoint_stats([("session-1", "run-1", "checkpoint-1")])


@pytest.mark.asyncio
async def test_checkpoint_stats_postgres_ignores_malformed_canonical_timestamp(
    temporary_postgres,
) -> None:
    """The production SQL path must survive malformed canonical timestamp text."""

    import asyncpg

    from ksadk.sessions.postgres_service import PostgresSessionService

    database = f"checkpoint_audit_{uuid4().hex}"
    admin = await asyncpg.connect(temporary_postgres.get_uri())
    await admin.execute(f'CREATE DATABASE "{database}"')
    await admin.close()
    service = PostgresSessionService(dsn=temporary_postgres.get_uri(database))
    try:
        await service.create_session("agent-1", "user-1", session_id="session-1")
        await RuntimeEventStore(service).append("session-1", [_continuation_created()])
        malformed = runtime_event_to_session_event(
            "session-1", _continuation_resumed().model_copy(update={"event_id": "malformed-pg"})
        )
        malformed.content["runtime_event"]["timestamp"] = "1e999999999999999999999"
        malformed.timestamp = 50.0
        await service.append_event("session-1", malformed)
        numeric = runtime_event_to_session_event(
            "session-1", _continuation_resumed().model_copy(update={"event_id": "numeric-pg"})
        )
        numeric.content["runtime_event"]["timestamp"] = "102.5"
        numeric.timestamp = 50.0
        await service.append_event("session-1", numeric)
        stale_carrier = runtime_event_to_session_event(
            "session-1", _continuation_resumed().model_copy(update={"event_id": "stale-pg"})
        )
        stale_carrier.content["runtime_event"]["timestamp"] = 101.0
        stale_carrier.timestamp = 1000.0
        await service.append_event("session-1", stale_carrier)

        stats = await service.get_checkpoint_stats(
            [("session-1", "run-1", "checkpoint-1")]
        )
        assert stats["audits"][("session-1", "run-1", "checkpoint-1")] == {
            "resume_count": 3,
            "last_resumed_at": 102.5,
        }
        lookup = await service.get_checkpoint_lookup_stats(
            "session-1", "run-1", "checkpoint-1"
        )
        assert lookup["candidate"] is not None
        assert lookup["candidate"].event_type == "continuation.created"
        assert lookup["candidate"].seq_id == 1
        assert lookup["max_seq_id"] == 1
    finally:
        if service._pool is not None:
            await service._pool.close()
        admin = await asyncpg.connect(temporary_postgres.get_uri())
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_stats_prefers_payload_timestamp_over_stale_carrier() -> None:
    from ksadk.sessions._postgres_checkpoint_stats import get_checkpoint_stats

    class FakeConnection:
        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            if "canonical_carrier_timestamps" in query:
                return [
                    {
                        "session_id": "session-1",
                        "run_id": "run-1",
                        "checkpoint_id": "checkpoint-1",
                        "legacy_last_resumed_at": None,
                        "canonical_carrier_timestamps": [1000.0],
                        "canonical_payloads": [
                            {
                                "schema_version": 2,
                                "event_id": "resume-1",
                                "event_type": "continuation.resumed",
                                "seq": 2,
                                "timestamp": 101.0,
                                "run_id": "run-1",
                                "scope_id": "run:run-1",
                                "source": {"framework": "langgraph"},
                                "continuation_id": "checkpoint-1",
                                "continuation_kind": "graph_checkpoint",
                                "resume_attempt_id": "attempt-1",
                            }
                        ],
                        "legacy_resume_count": 0,
                    }
                ]
            return [
                {
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "latest_seq_id": 1,
                }
            ]

    class FakePool:
        def __init__(self) -> None:
            self.connection = FakeConnection()

        async def acquire(self) -> FakeConnection:
            return self.connection

        async def release(self, connection: FakeConnection) -> None:
            return None

    class FakeSnapshot:
        def get(self):
            return None

    class FakeService:
        namespace = "default"
        _pool = FakePool()
        _checkpoint_snapshot_connection = FakeSnapshot()

        async def _ensure_schema(self) -> None:
            return None

    stats = await get_checkpoint_stats(
        FakeService(), [("session-1", "run-1", "checkpoint-1")]
    )
    assert stats["audits"][("session-1", "run-1", "checkpoint-1")] == {
        "resume_count": 1,
        "last_resumed_at": 101.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_checkpoint_stats_fails_loud_on_canonical_event_type_mismatch(
    tmp_path, backend: str
) -> None:
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(tmp_path / "sessions.sqlite")
    )
    await service.create_session("agent-1", "user-1", session_id="session-1")
    mismatched = runtime_event_to_session_event("session-1", _continuation_resumed())
    mismatched.content["runtime_event"]["event_type"] = "continuation.created"
    await service.append_event("session-1", mismatched)

    with pytest.raises(ValueError, match="event type"):
        await service.get_checkpoint_stats([("session-1", "run-1", "checkpoint-1")])
    with pytest.raises(ValueError, match="event type"):
        await service.get_checkpoint_lookup_stats("session-1", "run-1", "checkpoint-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
@pytest.mark.parametrize("corruption", ["storage", "metadata", "sequence"])
async def test_checkpoint_stats_fails_loud_on_canonical_identity_corruption(
    tmp_path, backend: str, corruption: str
) -> None:
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(tmp_path / "sessions.sqlite")
    )
    await service.create_session("agent-1", "user-1", session_id="session-1")
    event = runtime_event_to_session_event("session-1", _continuation_resumed())
    if corruption == "storage":
        event.id = "wrong-storage-id"
    elif corruption == "metadata":
        event.metadata["canonical_event_id"] = "wrong-event-id"
    else:
        event.seq_binding = None
        event.content["runtime_event"]["seq"] = 99
    await service.append_event("session-1", event)

    with pytest.raises(ValueError):
        await service.get_checkpoint_stats([("session-1", "run-1", "checkpoint-1")])
    with pytest.raises(ValueError):
        await service.get_checkpoint_lookup_stats("session-1", "run-1", "checkpoint-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_checkpoint_lookup_fails_loud_on_corrupt_creation_carrier(
    tmp_path, backend: str
) -> None:
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(tmp_path / "sessions.sqlite")
    )
    await service.create_session("agent-1", "user-1", session_id="session-1")
    event = runtime_event_to_session_event("session-1", _continuation_created())
    event.id = "wrong-storage-id"
    await service.append_event("session-1", event)

    with pytest.raises(ValueError):
        await service.get_checkpoint_lookup_stats("session-1", "run-1", "checkpoint-1")


@pytest.mark.asyncio
async def test_postgres_stats_fails_loud_on_corrupt_canonical_carrier() -> None:
    from ksadk.sessions._postgres_checkpoint_stats import get_checkpoint_stats

    event = runtime_event_to_session_event("session-1", _continuation_resumed())
    event.content["runtime_event"]["event_type"] = "continuation.created"
    raw = {
        "id": event.id,
        "event_type": event.event_type,
        "content": event.content,
        "timestamp": event.timestamp,
        "seq_id": event.seq_id,
        "invocation_id": event.invocation_id,
        "metadata": event.metadata,
    }

    class FakeConnection:
        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            if "canonical_carrier_timestamps" in query:
                return [
                    {
                        "session_id": "session-1",
                        "run_id": "run-1",
                        "checkpoint_id": "checkpoint-1",
                        "legacy_resume_count": 0,
                        "legacy_last_resumed_at": None,
                        "canonical_carrier_timestamps": [],
                        "canonical_timestamps": [],
                        "canonical_resume_rows": [raw],
                    }
                ]
            return [
                {
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "latest_seq_id": 0,
                }
            ]

    class FakePool:
        def __init__(self) -> None:
            self.connection = FakeConnection()

        async def acquire(self) -> FakeConnection:
            return self.connection

        async def release(self, connection: FakeConnection) -> None:
            return None

    class FakeSnapshot:
        def get(self):
            return None

    class FakeService:
        namespace = "default"
        _pool = FakePool()
        _checkpoint_snapshot_connection = FakeSnapshot()

        async def _ensure_schema(self) -> None:
            return None

    with pytest.raises(ValueError, match="event type"):
        await get_checkpoint_stats(FakeService(), [("session-1", "run-1", "checkpoint-1")])
