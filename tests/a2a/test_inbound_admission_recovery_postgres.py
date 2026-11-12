# ruff: noqa: F811
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from ksadk.a2a.control_plane import A2AControlPlaneError
from ksadk.a2a.inbound_kernel import InboundKernelSubmission, ManagedKernelRequestError
from ksadk.a2a.kernel_task_adapter import ManagedKernelA2ATaskAdapter
from ksadk.kernel.contracts import AgentControlReceipt, ControlError
from tests.a2a.test_inbound_kernel import identity
from tests.kernel.store_conformance import SESSION, command
from tests.kernel.test_postgres_store import pg_dsn, pg_store  # noqa: F401
from tests.kernel.test_postgres_store import kernel_postgres_server_dsn  # noqa: F401


def context():
    return SimpleNamespace(task_id='one-remote-task', context_id='one-context', current_task=None,
        call_context=SimpleNamespace(state={'a2a_identity':identity({'message':{
            'messageId':'one-original-message','parts':[{'text':'read real research'}],
        }})}))


def bridge(server):
    return ManagedKernelA2ATaskAdapter(object(), control_plane=server, session_events=object())


@pytest.mark.asyncio
@pytest.mark.parametrize('lose_receipt',[False,True])
async def test_same_admission_command_recovery_creates_one_real_pg_inbox(pg_store, lose_receipt):
    cmd=command('same-a2a-task-command','one original message')
    calls=[]
    class Server:
        async def submit_inbound_message(self,**args):
            calls.append(args)
            if len(calls)==1 and not lose_receipt:
                return InboundKernelSubmission(SESSION,'ar-target',AgentControlReceipt(
                    command_id=cmd.command_id,status='rejected',error=ControlError(
                        code='runtime_not_ready',message='not ready',retryable=False)))
            receipt=await pg_store.accept_command(cmd,queue_limit=100)
            if len(calls)==1:
                raise httpx.ReadError('receipt lost after durable acceptance')
            return InboundKernelSubmission(SESSION,'ar-target',receipt)
    result=await bridge(Server())._submit(context())
    assert result.receipt.status==('duplicate' if lose_receipt else 'accepted')
    assert len(calls)==2 and calls[0]==calls[1]
    assert calls[0]['remote_task_id']=='one-remote-task'
    assert result.receipt.command_id==cmd.command_id
    rows=await pg_store.list_messages(cmd.agent_instance_id,session_id=SESSION)
    assert len(rows)==1
    assert rows[0].command.command_id==cmd.command_id


@pytest.mark.asyncio
@pytest.mark.parametrize('error',[
    A2AControlPlaneError(code=403,message='denied',error_code='FORBIDDEN'),
    A2AControlPlaneError(code=502,message='invalid',error_code='A2A_CONTROL_PLANE_INVALID_RESPONSE'),
])
async def test_permanent_control_errors_are_not_retried(error):
    calls=[]
    class Server:
        async def submit_inbound_message(self,**args):calls.append(args);raise error
    with pytest.raises(ManagedKernelRequestError):await bridge(Server())._submit(context())
    assert len(calls)==1


@pytest.mark.asyncio
async def test_recovery_does_not_accept_changed_command_identity():
    calls=[]
    class Server:
        async def submit_inbound_message(self,**args):
            calls.append(args)
            return InboundKernelSubmission('same-session','ar-target',AgentControlReceipt(
                command_id=uuid4(),status='rejected',error=ControlError(
                    code='runtime_not_ready',message='not ready',retryable=False)))
    with pytest.raises(PermissionError,match='command identity'):
        await bridge(Server())._submit(context())
    assert len(calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel',[False,True])
async def test_one_admission_clock_and_cancellation(monkeypatch,cancel):
    monkeypatch.setenv('KSADK_A2A_INBOUND_ADMISSION_SECONDS','.12');calls=[];command_id=uuid4()
    class Server:
        async def submit_inbound_message(self,**args):
            calls.append(args)
            return InboundKernelSubmission('same-session','ar-target',AgentControlReceipt(
                command_id=command_id,status='rejected',error=ControlError(
                    code='runtime_not_ready',message='not ready',retryable=False)))
    start=asyncio.get_running_loop().time();task=asyncio.create_task(bridge(Server())._submit(context()))
    if cancel:
        await asyncio.sleep(.03);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    else:
        with pytest.raises(ManagedKernelRequestError):await task
    elapsed=asyncio.get_running_loop().time()-start
    assert len(calls)==1 and elapsed<.3
    if not cancel:assert elapsed>=.1
