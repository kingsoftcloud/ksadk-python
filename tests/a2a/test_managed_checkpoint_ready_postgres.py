import asyncio
import os
import time
from types import SimpleNamespace
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import StateGraph, START, END

from ksadk.a2a.langgraph_recovery import managed_checkpoint
from ksadk.harness.engine.postgres_checkpointer import create_bounded_postgres_saver


class State(TypedDict):
    value: str


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("KSADK_TEST_POSTGRES_DSN"), reason="requires real PostgreSQL")
async def test_publication_waits_for_real_pending_pg_checkpoint_without_running_tools():
    dsn=os.environ['KSADK_TEST_POSTGRES_DSN'];saver,pool=await create_bounded_postgres_saver(dsn)
    calls=[]
    def pending(state):calls.append(state);return state
    graph=StateGraph(State);graph.add_node('pending',pending);graph.add_edge(START,'pending');graph.add_edge('pending',END);graph=graph.compile(checkpointer=saver)
    config={'configurable':{'thread_id':'checkpoint-ready-'+uuid4().hex}}
    await graph.aupdate_state(config,{'value':'x'*(2*1024*1024)},as_node='pending')
    reads=[]
    class Runner:
        _agent=graph
        def describe_checkpoint_capability(self):return {'Supported':True,'SharedAcrossPods':True}
        async def _get_session_config(self,sid):return config
        async def _read_graph_state(self,config):
            reads.append(1)
            return await graph.aget_state(config),None
        def _checkpoint_ref_from_state(self,state):return {'langgraph':{'checkpoint_id':state.config.get('configurable',{}).get('checkpoint_id')}}
    async def publish():
        await asyncio.sleep(.55)
        return await graph.aupdate_state(config,{'value':'durable'},as_node=START)
    task=asyncio.create_task(publish())
    try:
        event=await managed_checkpoint(Runner(),SimpleNamespace(session_id='session',run_id='run',publication_epoch=0),SimpleNamespace(event_type='interaction.requested',_invocation=object(),timestamp=time.time()))
        stored=await task
        assert event.ref['checkpoint_id']==stored['configurable']['checkpoint_id']
        assert (await saver.aget_tuple(stored)).checkpoint['channel_values']['value']=='durable'
        assert calls==[] and len(reads)<=2
    finally:await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel',[False,True])
async def test_publication_deadline_and_cancellation_never_execute_graph(monkeypatch,cancel):
    monkeypatch.setenv('KSADK_A2A_CHECKPOINT_READY_SECONDS','.05');calls=[]
    class Runner:
        def describe_checkpoint_capability(self):return {'Supported':True,'SharedAcrossPods':True}
        async def _get_session_config(self,sid):return {}
        async def _read_graph_state(self,config):calls.append(1);return None,None
        def _checkpoint_ref_from_state(self,state):return {}
    task=asyncio.create_task(managed_checkpoint(Runner(),SimpleNamespace(session_id='session'),SimpleNamespace(event_type='interaction.requested',_invocation=object())))
    if cancel:
        await asyncio.sleep(.01);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    else:
        with pytest.raises(RuntimeError,match='CHECKPOINT_REQUIRED'):await task
    assert len(calls)==1


@pytest.mark.asyncio
async def test_publication_keeps_original_invocation_deadline(monkeypatch):
    monkeypatch.setenv('KSADK_A2A_CHECKPOINT_READY_SECONDS','1');calls=[]
    class Runner:
        def describe_checkpoint_capability(self):return {'Supported':True,'SharedAcrossPods':True}
        async def _get_session_config(self,sid):return {}
        async def _read_graph_state(self,config):calls.append(1);return None,None
        def _checkpoint_ref_from_state(self,state):return {}
    start=asyncio.get_running_loop().time()
    event=SimpleNamespace(event_type='interaction.requested',_invocation=SimpleNamespace(deadline_unix_seconds=time.time()+.04))
    with pytest.raises(RuntimeError,match='CHECKPOINT_REQUIRED'):
        await managed_checkpoint(Runner(),SimpleNamespace(session_id='session'),event)
    assert len(calls)==1 and asyncio.get_running_loop().time()-start<.15
