"""Run against a real disposable PostgreSQL server (optional test dependency)."""

import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest

from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.postgres_store import (
    PostgresTeamsStore,
    authority_schema,
    initialize_schema,
)
from ksadk.plugins.teams.store import QuotaScope, TeamsStore


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
    with pytest.raises(psycopg.OperationalError):
        with stores[0].transaction() as tx:
            pid = tx.connection.raw.info.backend_pid
            with stores[1]._pool.connection() as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
            tx.connection.execute("SELECT 1")
    assert stores[0].mutate("create", "stable", {}, operation) == first
    assert len(calls) == 1 and len(stores[0].events("g")) == 1


@pytest.fixture
def shared_pool(stores):
    from psycopg_pool import ConnectionPool

    with stores[0]._pool.connection() as connection:
        dsn = connection.info.dsn
    with ConnectionPool(dsn, min_size=1, max_size=2, kwargs={"autocommit": True}) as pool:
        yield pool


def test_shared_pool_resets_search_path_and_stays_bounded(shared_pool):
    values = []
    for index in range(12):
        authority = f"owner-{index}"
        initialize_schema(shared_pool, authority_ref=authority)
        values.append(PostgresTeamsStore(pool=shared_pool, authority_ref=authority))

    def write(index):
        with values[index].transaction() as tx:
            tx.put("group", "same-id", {"owner": index})
            assert tx.connection.execute("SHOW search_path").fetchone()[0] == values[index].schema

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(write, range(len(values))))
    for index, store in enumerate(values):
        with store.transaction() as tx:
            assert tx.get("group", "same-id")["owner"] == index
        store.close()
    with shared_pool.connection() as connection:
        assert not connection.execute("SHOW search_path").fetchone()[0].startswith("teams_")
    assert shared_pool.get_stats()["pool_size"] <= 2


def test_runtime_role_uses_existing_schema_without_ddl(shared_pool):
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import ConnectionPool

    authority = "restricted"
    initialize_schema(shared_pool, authority_ref=authority)
    schema, _ = authority_schema(authority)
    role = "teams_test_" + uuid4().hex[:12]
    with shared_pool.connection() as connection:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        connection.execute(
            sql.SQL("GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        dsn = make_conninfo(connection.info.dsn, user=role)
    with ConnectionPool(dsn, min_size=0, max_size=1, kwargs={"autocommit": True}) as restricted:
        store = PostgresTeamsStore(pool=restricted, authority_ref=authority, initialize=False)
        with store.transaction() as tx:
            tx.put("group", "g", {"groupId": "g"})
        store.close()
        with pytest.raises(TeamsError, match="初始化"):
            PostgresTeamsStore(pool=restricted, authority_ref="not-provisioned", initialize=False)


def test_two_authorities_compete_for_one_quota_and_rollback_atomically(shared_pool):
    with shared_pool.connection() as connection:
        connection.execute(
            "CREATE TABLE public.teams_quotas(scope_type TEXT,scope_id TEXT,resource TEXT,"
            'reserved BIGINT NOT NULL,"limit" BIGINT NOT NULL,revision BIGINT NOT NULL,'
            "PRIMARY KEY(scope_type,scope_id,resource))"
        )
    quota = QuotaScope("account", "same-account", "native_run", 1)
    values = []
    for authority in ("quota-a", "quota-b"):
        initialize_schema(shared_pool, authority_ref=authority)
        values.append(PostgresTeamsStore(pool=shared_pool, authority_ref=authority))

    def reserve(store):
        with store.transaction(quota_scopes=[quota]) as tx:
            reserved, limit = tx.connection.execute(
                'SELECT reserved,"limit" FROM public.teams_quotas WHERE scope_id=?',
                [quota.scope_id],
            ).fetchone()
            if reserved >= limit:
                return False
            tx.connection.execute(
                "UPDATE public.teams_quotas SET reserved=reserved+1 WHERE scope_id=?",
                [quota.scope_id],
            )
            tx.put("delivery", "d", {"status": "pending"})
            tx.event("g", "delivery.created", {})
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, values))
    assert sorted(results) == [False, True]
    with pytest.raises(ValueError, match="rollback"):
        with values[0].transaction(quota_scopes=[quota]) as tx:
            tx.connection.execute(
                "UPDATE public.teams_quotas SET reserved=0 WHERE scope_id=?", [quota.scope_id]
            )
            tx.put("delivery", "rolled-back", {"status": "pending"})
            raise ValueError("rollback")
    with values[0].transaction(quota_scopes=[quota]) as tx:
        assert tx.get("delivery", "rolled-back", required=False) is None
        assert (
            tx.connection.execute(
                "SELECT reserved FROM public.teams_quotas WHERE scope_id=?", [quota.scope_id]
            ).fetchone()[0]
            == 1
        )
    with values[0].transaction():
        with pytest.raises(TeamsError, match="最外层"):
            with values[0].transaction(quota_scopes=[quota]):
                pass


def test_commit_ack_loss_does_not_replay_operation(shared_pool):
    import psycopg

    class LoseCommitAck:
        lose = False

        @contextmanager
        def connection(self):
            with shared_pool.connection() as connection:
                yield connection
            if self.lose:
                self.lose = False
                raise psycopg.OperationalError("injected lost COMMIT acknowledgement")

    initialize_schema(shared_pool, authority_ref="commit-ack")
    pool = LoseCommitAck()
    store = PostgresTeamsStore(pool=pool, authority_ref="commit-ack")
    calls = []

    def operation(tx):
        calls.append(1)
        return tx.event("g", "created", {})

    pool.lose = True
    with pytest.raises(psycopg.OperationalError):
        store.mutate("create", "stable", {}, operation)
    result = store.mutate("create", "stable", {}, operation)
    assert result["groupSeq"] == 1 and len(calls) == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_indexed_pages_due_queries_and_nested_transactions(backend, tmp_path, shared_pool):
    if backend == "sqlite":
        store = TeamsStore(tmp_path / "teams.sqlite")
    else:
        initialize_schema(shared_pool, authority_ref="queries")
        store = PostgresTeamsStore(pool=shared_pool, authority_ref="queries")
    try:
        with store.transaction() as tx:
            with store.transaction() as same:
                assert same is tx
            for index in range(5):
                tx.put(
                    "delivery",
                    str(index),
                    {
                        "groupId": "g",
                        "teamRunId": "r",
                        "status": "pending",
                        "_nextRetryAt": index,
                        "index": index,
                    },
                )
            tx.put(
                "delivery",
                "terminal",
                {"groupId": "g", "status": "accepted", "_terminalState": "succeeded"},
            )
        page = store.list_page("delivery", "g", team_run_id="r", limit=2)
        assert [item["index"] for item in page.items] == [0, 1]
        second = store.list_page("delivery", "g", team_run_id="r", limit=2, after=page.next_cursor)
        assert [item["index"] for item in second.items] == [2, 3]
        assert [item["index"] for item in store.list_due("delivery", due_before=2, limit=2)] == [
            0,
            1,
        ]
        assert store.list_due("delivery", due_before=20, statuses=["accepted"]) == []
        assert [item["index"] for item in store.recent("delivery", "g", 2, team_run_id="r")] == [
            3,
            4,
        ]
        with pytest.raises(TeamsError, match="嵌套事务失败"):
            with store.transaction() as tx:
                tx.put("group", "rolled-back", {})
                try:
                    with store.transaction():
                        raise ValueError("nested")
                except ValueError:
                    pass
        with store.transaction() as tx:
            assert tx.get("group", "rolled-back", required=False) is None
    finally:
        store.close()


def test_sqlite_existing_v1_projection_backfill(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE teams_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO teams_meta VALUES('schema_version','1');
            CREATE TABLE team_objects(ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,object_id TEXT NOT NULL,group_id TEXT,team_run_id TEXT,
                revision INTEGER NOT NULL,body TEXT NOT NULL,UNIQUE(kind,object_id));
            INSERT INTO team_objects(kind,object_id,group_id,revision,body)
                VALUES('delivery','old','g',1,'{"status":"pending","_nextRetryAt":3}');
        """)
    store = TeamsStore(path)
    try:
        assert len(store.list_due("delivery", due_before=3)) == 1
        assert store.list_due("delivery", due_before=2) == []
    finally:
        store.close()


def _process_write(dsn, authority, count):
    store = PostgresTeamsStore(dsn, authority_ref=authority, initialize=False)
    try:
        for _ in range(count):
            with store.transaction() as tx:
                tx.event("process-group", "created", {})
    finally:
        store.close()


def test_real_processes_share_authority_lock(shared_pool):
    initialize_schema(shared_pool, authority_ref="processes")
    with shared_pool.connection() as connection:
        dsn = connection.info.dsn
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_process_write, args=(dsn, "processes", 8)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    store = PostgresTeamsStore(pool=shared_pool, authority_ref="processes")
    assert [item["groupSeq"] for item in store.events("process-group")] == list(range(1, 17))


def test_canonical_authority_uses_its_identity_digest():
    authority = "ta_" + "ab" * 32
    schema, _ = authority_schema(authority)
    assert schema == "teams_" + "ab" * 12
