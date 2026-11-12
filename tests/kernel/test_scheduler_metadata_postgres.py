import asyncio
import json
import os
import time
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from ksadk.kernel.bootstrap import build_agent_kernel_runtime
from ksadk.kernel.postgres_store import PostgresAgentKernelStore, PostgresKernelEventLog, PostgresFencedSessionEventStore
from ksadk.sessions.postgres_service import PostgresSessionService
from tests.kernel.control_harness import kernel_stack, command
from tests.kernel.test_production_bootstrap import _runtime_config


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get('KSADK_TEST_POSTGRES_DSN'),reason='real PostgreSQL required')
async def test_scheduler_and_health_never_load_completed_payloads_or_scan_each_session(monkeypatch):
    dsn=os.environ['KSADK_TEST_POSTGRES_DSN'];agent='metadata-'+uuid4().hex
    service=PostgresSessionService(dsn=dsn,namespace=agent);await service.create_session(agent_id=agent,user_id='test',session_id='open')
    await service.create_session(agent_id=agent,user_id='test',session_id='pending')
    log=PostgresKernelEventLog(service._pool,namespace=agent);store=PostgresAgentKernelStore(service._pool,log);await store.ensure_schema()
    stack=await kernel_stack();runtime=build_agent_kernel_runtime(_runtime_config(stack,agent_instance_id=agent,dsn=dsn,store=store,session_events=PostgresFencedSessionEventStore(store),session_service=service,pool=service._pool,activation_id=agent+'-pod'))
    payload=command().model_dump_json()
    async with service._pool.acquire() as c:
        rows=[(uuid4(),agent,'closed-'+str(i),agent+'-key-'+str(i),i,'completed',json.dumps({'unused_large_payload':'x'*65536})) for i in range(120)]
        rows += [(uuid4(),agent,s,agent+'-'+s,200+i,status,payload) for i,(s,status) in enumerate([('queued','accepted'),('claimed','claimed'),('open','completed'),('ended','completed'),('pending','completed')])]
        await c.executemany('INSERT INTO kernel_inbox(message_id,agent_instance_id,session_id,idempotency_key,request_digest,accepted_seq,status,payload) VALUES($1,$2,$3,$4,\'test\',$5,$6,$7::jsonb)',rows)
        await c.executemany('INSERT INTO kernel_runs(run_id,agent_instance_id,session_id,state) VALUES($1,$2,$3,$4)',[(agent+'-'+s,agent,s,state) for s,state in [('open','waiting'),('ended','completed'),('pending','pending'),('orphan','running')]])
    try:
        assert (await store.find_active_run(agent,'open')).state.value=='waiting'
        assert await store.find_active_run(agent,'ended') is None
        assert await store.find_active_run(agent,'pending') is None
        monkeypatch.setattr(store,'list_messages',AsyncMock(side_effect=AssertionError('full Inbox read is forbidden')))
        monkeypatch.setattr(store,'find_active_run',AsyncMock(side_effect=AssertionError('N+1 history scans are forbidden')))
        monkeypatch.setattr(store,'current_lease',AsyncMock(side_effect=AssertionError('N+1 lease checks are forbidden')))
        # LeaseHeartbeat itself legitimately checks a lease when taking ownership.
        original=type(store).current_lease.__get__(store,type(store));monkeypatch.setattr(store,'current_lease',original)
        lease,_=await runtime.lease_heartbeat.ensure_lease('open');assert lease
        monkeypatch.setattr(store,'current_lease',AsyncMock(side_effect=AssertionError('N+1 lease checks are forbidden')))
        runtime._heartbeat_sessions.add('open');runtime._worker_running=True;runtime._last_scheduler_poll=time.monotonic()
        assert await runtime._pending_sessions()=={'queued','claimed','open','orphan'}
        health=await runtime.readiness.check();assert health['store_ok'] and health['lease_healthy'] and health['ready']
        async with service._pool.acquire() as c:
            await c.execute('UPDATE kernel_activations SET fencing_token=fencing_token+1,activation_id=$2 WHERE agent_instance_id=$1',agent,'foreign-owner')
        health=await runtime.readiness.check();assert not health['lease_healthy'] and not health['ready']
        assert await runtime._store_reachable()
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_transient_scheduler_outage_recovers_same_accepted_command_without_sticky_degradation(monkeypatch):
    stack=await kernel_stack();await stack.kernel.submit(command(idempotency_key='outage-once'),permit=stack.permit('enqueue'))
    original=stack.store.list_messages;attempts=0
    async def flaky(*args,**kwargs):
        nonlocal attempts
        attempts+=1
        if attempts<=6:raise TimeoutError('temporary read outage')
        return await original(*args,**kwargs)
    monkeypatch.setattr(stack.store,'list_messages',flaky)
    runtime=build_agent_kernel_runtime(_runtime_config(stack,poll_interval=.005,store_failure_degrade_threshold=2))
    try:
        await runtime.start()
        async with asyncio.timeout(4):
            while not stack.adapter.start_requests:await asyncio.sleep(.01)
        assert attempts>6 and not runtime.degraded and runtime.worker_running
        assert len(stack.adapter.start_requests)==1
        assert len(await original(runtime.config.agent_instance_id))==1
    finally:await runtime.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get('KSADK_TEST_POSTGRES_DSN'),reason='real PostgreSQL required')
async def test_bulk_renewal_keeps_owner_fence_expiration_and_release_guards(monkeypatch):
    agent='bulk-'+uuid4().hex;service=PostgresSessionService(dsn=os.environ['KSADK_TEST_POSTGRES_DSN'],namespace=agent)
    await service.create_session(agent_id=agent,user_id='test',session_id='valid')
    log=PostgresKernelEventLog(service._pool,namespace=agent);store=PostgresAgentKernelStore(service._pool,log);await store.ensure_schema();stack=await kernel_stack()
    runtime=build_agent_kernel_runtime(_runtime_config(stack,agent_instance_id=agent,store=store,session_events=PostgresFencedSessionEventStore(store),session_service=service,pool=service._pool,activation_id=agent+'-pod'))
    try:
        leases={}
        for sid in ['valid','bad-owner','bad-fence','expired','released']:
            leases[sid]=(await runtime.lease_heartbeat.ensure_lease(sid))[0]
        async with service._pool.acquire() as c:
            await c.execute("UPDATE kernel_activations SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE agent_instance_id=$1 AND session_id='expired'",agent)
        await store.release_activation(leases['released'].activation_id,expected_fence=leases['released'].fencing_token)
        owned={s:(l.activation_id,l.fencing_token) for s,l in leases.items()};owned['bad-owner']=('foreign',leases['bad-owner'].fencing_token);owned['bad-fence']=(leases['bad-fence'].activation_id,leases['bad-fence'].fencing_token+1)
        assert await store.renew_owned_activations(agent,owned,lease_ttl_seconds=45)=={'valid'}
        valid=await store.current_lease(agent,'valid');assert valid.fencing_token==leases['valid'].fencing_token and valid.activation_id==leases['valid'].activation_id
        assert await store.current_lease(agent,'expired') is None and await store.current_lease(agent,'released') is None
        for ttl in [0,-1,float('nan'),float('inf')]:
            with pytest.raises(ValueError):await store.renew_owned_activations(agent,owned,lease_ttl_seconds=ttl)
        # A healthy runtime renews its valid set without per-session acquire calls.
        runtime._heartbeat_sessions={'valid','bad-owner','bad-fence'};runtime._last_renewed={}
        monkeypatch.setattr(store,'acquire_activation',AsyncMock(side_effect=AssertionError('valid lease must use bulk renewal')))
        await runtime._renew_leased_sessions()
        assert set(runtime._last_renewed)==runtime._heartbeat_sessions
        assert not store.acquire_activation.called
    finally:await service.aclose()
