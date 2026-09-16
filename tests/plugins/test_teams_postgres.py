"""Run against a real disposable PostgreSQL server (optional test dependency)."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.postgres_store import PostgresTeamsStore


@pytest.fixture
def stores(tmp_path, monkeypatch):
    pgserver = pytest.importorskip("pgserver")
    pytest.importorskip("psycopg")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    server = pgserver.get_server(tmp_path / "postgres", cleanup_mode="stop")
    values = [PostgresTeamsStore(server.get_uri(), authority_ref="test") for _ in range(2)]
    try:
        yield values
    finally:
        for value in values:
            value.close()
        server.cleanup()


def test_postgres_shared_transaction_lock_and_monotonic_events(stores):
    def write(index):
        store = stores[index % 2]
        with store.transaction() as tx:
            group = tx.get("group", "g", required=False) or {"groupId": "g", "revision": 0}
            group["revision"] += 1
            tx.put("group", "g", group)
            tx.event("g", "group.updated", group)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(24)))
    with stores[0].transaction() as tx:
        assert tx.get("group", "g")["revision"] == 24
    assert [event["groupSeq"] for event in stores[1].events("g")] == list(range(1, 25))


def test_postgres_idempotency_is_shared_between_authority_instances(stores):
    def invoke(index):
        def operation(tx):
            event = tx.event("g", "created", {})
            return {"eventId": event["eventId"]}

        return stores[index % 2].mutate("create", "same-request", {"name": "team"}, operation)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, range(12)))
    assert all(result == results[0] for result in results)
    assert len(stores[0].events("g")) == 1
    with pytest.raises(TeamsError, match="相同幂等键"):
        stores[1].mutate("create", "same-request", {"name": "changed"}, lambda tx: {})


def test_postgres_failed_operation_rolls_back_state_event_and_receipt(stores):
    def failure(tx):
        tx.put("group", "g", {"groupId": "g"})
        tx.event("g", "created", {})
        raise ValueError("injected transaction failure")

    with pytest.raises(ValueError):
        stores[0].mutate("create", "retryable", {}, failure)
    with stores[1].transaction() as tx:
        assert tx.get("group", "g", required=False) is None
        assert tx.watermark("g") == 0
    assert stores[1].mutate("create", "retryable", {}, lambda tx: {"ok": True}) == {"ok": True}


def test_postgres_reconnects_after_connection_loss_without_replaying_transaction(stores):
    import psycopg

    calls = []

    def operation(tx):
        calls.append(1)
        tx.event("g", "created", {})
        return {"accepted": True}

    first = stores[0].mutate("create", "stable", {}, operation)
    pid = stores[0]._raw.info.backend_pid
    stores[1]._raw.execute("SELECT pg_terminate_backend(%s)", (pid,))
    with pytest.raises(psycopg.OperationalError):
        stores[0].mutate("create", "stable", {}, operation)
    assert stores[0].mutate("create", "stable", {}, operation) == first
    assert len(calls) == 1 and len(stores[0].events("g")) == 1
