"""Real PostgreSQL coverage for the current MemoryProvider contract and legacy rows."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from ksadk.memory.models import (
    CoreMemoryRequest,
    MemoryConflictError,
    MemoryDeleteRequest,
    MemoryRecord,
    MemorySearchRequest,
)
from ksadk.memory.providers.local_postgres import _SCHEMA, PostgresMemoryProvider


@pytest.fixture
def database():
    dsn = os.getenv("KSADK_TEST_MEMORY_POSTGRES_DSN")
    if not dsn:
        pytest.skip("KSADK_TEST_MEMORY_POSTGRES_DSN requires isolated PostgreSQL")
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.sql import SQL, Identifier

    schema = "memory_contract_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        try:
            yield make_conninfo(dsn, options=f"-c search_path={schema}")
        finally:
            admin.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


def record(**updates):
    values = dict(
        memory_id="m1",
        tenant_id="t",
        workspace_id="w",
        scope="user",
        scope_id="u",
        memory_type="fact",
        content="SQL compatibility fact",
        summary="SQL fact",
        status="active",
        confidence=0.9,
        importance=0.8,
        valid_from="",
        valid_to="",
        expires_at="",
        source_session_id="session-1",
        source_event_ids=["event-1"],
        source_seq_range=(1, 2),
        content_hash="hash-1",
        version=1,
        metadata={"slot_key": "preference"},
        source_artifact_refs=("artifact://example",),
        source_artifacts=(
            {
                "uri": "artifact://example",
                "mime_type": "text/plain",
                "content_hash": "sha256:" + "a" * 64,
                "size_bytes": 10,
            },
        ),
    )
    values.update(updates)
    return MemoryRecord(**values)


def query(**updates):
    return MemorySearchRequest(query="SQL", scopes=[("user", "u")], memory_types=[], **updates)


@pytest.fixture
def provider(database):
    value = PostgresMemoryProvider(dsn=database, tenant_id="t", workspace_id="w")
    try:
        yield value
    finally:
        value.close()


def test_current_models_empty_results_and_crud(provider):
    assert provider.search(query()).status == "ok"
    assert not provider.delete(MemoryDeleteRequest("absent", "user", "u")).deleted
    saved = provider.upsert(record(), expected_version=None)
    assert provider.get("m1") == saved
    assert provider.search(query()).records == [saved]
    assert provider.list_core(CoreMemoryRequest([("user", "u")])) == [saved]
    assert saved.source_artifact_refs == ("artifact://example",)
    assert saved.source_seq_range == (1, 2)
    updated = provider.upsert(replace(saved, content="SQL updated"), expected_version=1)
    assert updated.version == 2 and updated.created_at == saved.created_at
    assert provider.delete(MemoryDeleteRequest("m1", "user", "u")).deleted
    assert provider.get("m1").status == "deleted"
    assert provider.search(query()).records == []
    assert provider.delete(MemoryDeleteRequest("m1", "user", "u", hard=True)).deleted
    assert provider.get("m1") is None


def test_scope_tenant_workspace_type_ttl_and_metadata_filters(provider, database):
    provider.upsert(record(), expected_version=None)
    provider.upsert(record(memory_id="other-user", scope_id="other"), expected_version=None)
    provider.upsert(
        record(memory_id="expired", expires_at="2000-01-01T00:00:00Z"), expected_version=None
    )
    provider.upsert(
        record(memory_id="episode", memory_type="episode", content_hash="episode"),
        expected_version=None,
    )
    assert {r.memory_id for r in provider.search(query()).records} == {"m1", "episode"}
    assert provider.search(replace(query(), scopes=[])).records == []
    assert provider.search(query(filters={"tenant_id": "someone-else"})).records == []
    assert provider.search(query(filters={"slot_key": "missing"})).records == []
    assert [
        r.memory_id for r in provider.search(replace(query(), memory_types=["fact"])).records
    ] == ["m1"]
    assert [r.memory_id for r in provider.list_core(CoreMemoryRequest([("user", "u")]))] == ["m1"]
    assert not provider.delete(MemoryDeleteRequest("m1", "user", "other", hard=True)).deleted
    other = PostgresMemoryProvider(dsn=database, tenant_id="another", workspace_id="w")
    try:
        assert other.get("m1") is None
        assert other.search(query()).records == []
        assert not other.delete(MemoryDeleteRequest("m1", "user", "u", hard=True)).deleted
    finally:
        other.close()


def test_cas_race_and_immutable_identity(provider, database):
    saved = provider.upsert(record(), expected_version=None)
    with pytest.raises(MemoryConflictError):
        provider.upsert(record(memory_id="absent"), expected_version=1)
    with pytest.raises(MemoryConflictError):
        provider.upsert(replace(saved, scope_id="changed"), expected_version=1)
    other = PostgresMemoryProvider(dsn=database, tenant_id="t", workspace_id="w")

    def update(p):
        try:
            return p.upsert(replace(saved, content="SQL race"), expected_version=1).version
        except MemoryConflictError:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, [provider, other]))
        assert sorted(map(str, outcomes)) == ["2", "conflict"]
        assert provider.get("m1").version == 2
    finally:
        other.close()


def test_legacy_schema_and_record_survive_additive_migration(database):
    import psycopg

    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute(_SCHEMA)
        connection.execute(
            "INSERT INTO memory_records (memory_id,tenant_id,workspace_id,scope,scope_id,"
            "content,created_at,updated_at) VALUES ('legacy','t','w','user','u',"
            "'SQL legacy fact',1700000000,1700000000)"
        )
    p = PostgresMemoryProvider(dsn=database, tenant_id="t", workspace_id="w")
    try:
        old = p.get("legacy")
        assert old.content == "SQL legacy fact" and old.version == 1
        assert old.created_at.startswith("2023-")
        assert p.search(query()).records[0].memory_id == "legacy"
        saved = p.upsert(replace(old, content="SQL upgraded fact"), expected_version=1)
        assert saved.version == 2 and saved.created_at == old.created_at
        assert p.get("legacy") == saved
    finally:
        p.close()


def test_query_failure_returns_safe_structured_result(provider):
    provider._conn.close()
    result = provider.search(query())
    assert result.status == "failed" and result.records == []
    assert result.error_code == "provider_error"
    assert provider.delete(MemoryDeleteRequest("m1", "user", "u")).status == "failed"
