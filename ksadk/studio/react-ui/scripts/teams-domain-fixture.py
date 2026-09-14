"""Generate public DTOs with the real domain/runtime and a controlled execution port.

No model/network or product seed data. Used by the cross-repository contract test.
"""
from __future__ import annotations
import asyncio
import json
import tempfile
from pathlib import Path
from ksadk.plugins.execution_host import ExecutionReceipt, ExecutionBarrier
from ksadk.plugins.teams.contracts import Actor, GroupCreateInput, MessageInput, TaskCreateInput
from ksadk.plugins.teams.runtime import TeamsRuntime

class ControlledHost:
    def __init__(self):
        self.receipts = {}
        self.sessions = {}
    async def ensure_session(self, scope):
        pass
    async def submit(self, scope, **payload):
        member = payload['policy_context']['memberId']
        self.sessions[scope.session_id] = member
        receipt = ExecutionReceipt('accepted', command_id='command-' + member, message_id='message-' + member, run_id='run-' + member, run_status='awaiting_approval' if member == 'engineer' else 'running')
        self.receipts[payload['idempotency_key']] = receipt
        return receipt
    async def lookup(self, scope, idempotency_key):
        return self.receipts.get(idempotency_key, ExecutionReceipt('missing'))
    async def set_grant(self, scope, grant_id, state, idempotency_key):
        return ExecutionBarrier(state, 1)
    async def cancel(self, scope, run_id, idempotency_key):
        return ExecutionReceipt('accepted', run_id=run_id)
    async def interactions(self, scope):
        if self.sessions.get(scope.session_id) != 'engineer':
            return []
        return [{'interaction_id': 'approval-one', 'run_id': 'run-engineer', 'revision': 1, 'kind': 'approval', 'status': 'pending', 'presentation': {'title': '确认写入', 'description': '检查真实投影字段'}, 'request_schema': None, 'created_at': '2026-09-11T00:00:00+00:00'}]
    async def get_interaction(self, scope, **kwargs):
        return None
    async def source_events(self, scope, *, after, limit):
        member = self.sessions.get(scope.session_id)
        if member != 'engineer' or after:
            return {'items': [], 'cursor': after}
        return {'items': [{'event_id': 'child-source-one', 'seq': 1, 'run_id': 'run-engineer', 'family': 'runtime', 'payload': {'source': {'native_run_id': 'child-review', 'metadata': {'parent_run_id': 'run-engineer', 'native_event_type': 'run.started', 'agent_id': 'Review'}}}}], 'cursor': 1}

async def main():
    owner = Actor('contract-tenant', 'contract-owner')
    binding = {'bindingRef': 'binding-local', 'providerRef': 'ksadk.harness@1', 'kind': 'local_build', 'agentId': 'contract-agent', 'buildId': 'build-local', 'capabilities': {'enqueue': True, 'leader': True, 'cancel': True, 'restore': True, 'steer': False, 'interaction': True}}
    with tempfile.TemporaryDirectory(prefix='teams-contract-') as directory:
        runtime = TeamsRuntime(path=Path(directory) / 'teams.sqlite', authority_ref='contract-local', host=ControlledHost())
        await runtime.start(background=False)
        try:
            domain = runtime.require_domain()
            created = domain.create_group(owner, GroupCreateInput(name='跨仓契约验证', members=[{'memberId': member, 'name': member, 'bindingRef': 'binding-local'} for member in ['leader', 'engineer']], leaderMemberId='leader', idempotencyKey='create-contract'), {'binding-local': binding})
            gid = created['group']['groupId']
            stages = [{'stage': 'created', 'snapshot': created}]
            receipt = domain.send(owner, gid, MessageInput(parts=[{'kind': 'text', 'text': '检查端到端协议'}], intent='start_goal', idempotencyKey='first-goal'))
            stages.append({'stage': 'first_goal', 'snapshot': domain.snapshot(owner, gid)})
            domain.create_task(owner, gid, receipt['teamRunId'], TaskCreateInput(title='验证接口', description='检查完整引用', ownerMemberId='engineer', acceptanceCriteria='协议兼容'), 'task-one')
            domain.create_task(owner, gid, receipt['teamRunId'], TaskCreateInput(title='待分派任务'), 'task-unassigned')
            stages.append({'stage': 'tasks', 'snapshot': domain.snapshot(owner, gid)})
            await runtime.tick()
            await runtime.tick()
            stages.append({'stage': 'approval_and_child', 'snapshot': domain.snapshot(owner, gid)})
            print(json.dumps({'stages': stages, 'events': domain.store.events(gid, after=0), 'execution': domain.execution(owner, gid, receipt['teamRunId'])}, ensure_ascii=False))
        finally:
            await runtime.close()

asyncio.run(main())
