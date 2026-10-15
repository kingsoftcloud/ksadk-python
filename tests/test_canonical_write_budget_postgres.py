"""Canonical write preflight and reconciliation share the write phase/deadline."""
import asyncio
import os
import time
import uuid

import pytest
import pytest_asyncio

from ksadk.events.canonical import RunStarted, SourceRef
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.sessions.db_budget import DatabaseBudget
from ksadk.sessions.postgres_service import PostgresSessionService, _TimedConnection


@pytest_asyncio.fixture
async def pg():
    dsn = os.getenv('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    service = PostgresSessionService(dsn=dsn, namespace='canonical_budget_'+uuid.uuid4().hex)
    await service.create_session('agent','owner','session')
    yield service
    service.db_budget = DatabaseBudget()
    await service.delete_session('session')
    await service.aclose()


def event():
    return RunStarted(schema_version=2,event_id=uuid.uuid4().hex,seq=0,timestamp=time.time(),
                      run_id='run',scope_id='scope',source=SourceRef(framework='langgraph'),status='running')


@pytest.mark.asyncio
async def test_idempotency_reads_use_write_budget_without_widening_public_reads(pg, monkeypatch):
    original = _TimedConnection.fetchrow
    async def delayed(connection, query, *args, **kwargs):
        if 'id = $3' in query:
            await connection.execute('SELECT pg_sleep(0.10)')
        return await original(connection,query,*args,**kwargs)
    monkeypatch.setattr(_TimedConnection,'fetchrow',delayed)
    pg.db_budget = DatabaseBudget(read_sql_seconds=.03,write_sql_seconds=.5,write_operation_seconds=2)
    store=RuntimeEventStore(pg);candidate=event()
    first,created=await store.persist_one('session',candidate)
    assert created and first.seq==1
    same,created=await store.persist_one('session',candidate)
    assert not created and same.event_id==candidate.event_id
    with pytest.raises(TimeoutError):
        await store.event_by_id('session',candidate.event_id)
    assert await pg.count_events('session')==1


@pytest.mark.asyncio
async def test_real_commit_receipt_loss_uses_write_lookup_without_reappend(pg,monkeypatch):
    pg.db_budget=DatabaseBudget(write_operation_seconds=2)
    append=pg.append_event;calls=0
    async def receipt_lost(*args,**kwargs):
        nonlocal calls
        calls+=1
        await append(*args,**kwargs)
        raise ConnectionError('test lost commit receipt')
    monkeypatch.setattr(pg,'append_event',receipt_lost)
    store=RuntimeEventStore(pg);candidate=event()
    confirmed,created=await store.persist_one('session',candidate)
    assert confirmed.event_id==candidate.event_id and confirmed.seq==1
    assert not created and calls==1
    assert await pg.count_events('session')==1
    conflict=candidate.model_copy(update={'run_id':'other-run'})
    with pytest.raises(ValueError):await store.persist_one('session',conflict)
    assert calls==1


@pytest.mark.asyncio
async def test_preflight_and_append_do_not_restart_operation_deadline(pg,monkeypatch):
    pg.db_budget=DatabaseBudget(write_sql_seconds=.5,write_operation_seconds=.2,cleanup_seconds=.2)
    read=pg.get_event_by_id_for_write;append=pg.append_event
    async def delayed_read(*args):
        async with pg._connection(write=True) as conn:await conn.execute('SELECT pg_sleep(.12)')
        return await read(*args)
    async def delayed_append(*args):
        async with pg._connection(write=True) as conn:await conn.execute('SELECT pg_sleep(.12)')
        return await append(*args)
    monkeypatch.setattr(pg,'get_event_by_id_for_write',delayed_read)
    monkeypatch.setattr(pg,'append_event',delayed_append)
    start=time.monotonic()
    with pytest.raises(TimeoutError):await RuntimeEventStore(pg).persist_one('session',event())
    assert time.monotonic()-start<.6
    assert await pg.count_events('session')==0
    async with pg._connection() as conn:
        assert not conn.is_in_transaction()
        assert await conn.fetchval('SELECT 1')==1
