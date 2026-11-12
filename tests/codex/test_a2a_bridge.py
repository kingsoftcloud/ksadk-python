import asyncio
import json
import queue
from concurrent.futures import ThreadPoolExecutor

import pytest

from ksadk.codex.a2a_tools import CodexExpertBridge, failed_tool_result
from ksadk.codex.client import AsyncCodexClient
from tests.a2a.test_codex_expert import call
from tests.a2a.test_invocation_service_r85 import request
from tests.codex.test_sdk_transport import _config


def active_bridge():
    bridge = CodexExpertBridge([request().spec])
    output = queue.Queue()
    bridge.register_thread('thread-1')
    bridge.activate('thread-1', output)
    return bridge, output


def test_callback_resolves_once_and_deduplicates():
    bridge, output = active_bridge()
    with ThreadPoolExecutor() as pool:
        first = pool.submit(bridge.request, call())
        event = output.get(timeout=2)
        assert event['params']['callId'] == 'item-1'
        conflict = bridge.request(call(arguments={'message': 'substituted'}))
        assert 'CONFLICT' in conflict['contentItems'][0]['text']
        response = failed_tool_result('test')
        assert bridge.resolve(call(), response)
        assert first.result(timeout=2) == response
        assert bridge.request(call()) == response
        assert output.empty()
        assert not bridge.resolve(call(), response)
    bridge.close()


@pytest.mark.parametrize('method', ['close', 'deactivate'])
def test_shutdown_releases_waiting_reader(method):
    bridge, output = active_bridge()
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(bridge.request, call())
        output.get(timeout=2)
        getattr(bridge, method)(*(['thread-1'] if method == 'deactivate' else []))
        assert 'CLOSED' in pending.result(timeout=2)['contentItems'][0]['text']
    assert not bridge.resolve(call(), failed_tool_result('late'))


@pytest.mark.parametrize('changes', [{'threadId': 'foreign'}, {'tool': 'foreign'}, {'namespace': 'foreign'}])
def test_foreign_request_never_reaches_owner(changes):
    bridge, output = active_bridge()
    assert not bridge.request(call(**changes))['success']
    assert output.empty()
    bridge.close()


@pytest.mark.asyncio
async def test_real_sdk_registers_fixed_expert_and_guards_unverified_resume(tmp_path):
    client = AsyncCodexClient(config=_config(tmp_path))
    client.configure_a2a_experts([request().spec])
    try:
        thread = await client.start_thread({'sandbox_read_only': True, 'approval_mode': 'manual',
                                          'base_instructions': 'Call the configured expert only.'})
        assert await client.resume_thread(thread) == thread
        with pytest.raises(ValueError, match='RESUME_UNVERIFIED'):
            await client.resume_thread('foreign')
        with pytest.raises(ValueError, match='GOAL_UNSUPPORTED'):
            async for _ in client.run_goal(thread, 'test'):
                pass
    finally:
        await asyncio.wait_for(client.close(), 5)
    records = [json.loads(line) for line in (tmp_path / 'requests.jsonl').read_text().splitlines()]
    started = next(item['params'] for item in records if item.get('method') == 'thread/start')
    assert started['baseInstructions'] == 'Call the configured expert only.'
    assert started['sandbox'] == 'read-only'
    assert started['approvalPolicy'] == 'on-request'
    assert started['approvalsReviewer'] == 'user'
    tool = started['dynamicTools'][0]
    assert tool['name'] == 'expert'
    assert set(tool['inputSchema']['properties']) == {'message'}
    assert not any(item.get('method') == 'thread/resume' for item in records)


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt', [False, True])
async def test_real_sdk_dynamic_callback_returns_result_and_interrupt_unblocks(tmp_path, interrupt):
    import sys
    from pathlib import Path
    from openai_codex import CodexConfig

    client = AsyncCodexClient(config=CodexConfig(launch_args_override=(
        sys.executable, str(Path(__file__).with_name('a2a_app_server.py')))))
    client.configure_a2a_experts([request().spec])
    response = {'success': True, 'contentItems': [{'type': 'inputText', 'text': 'expert reply'}]}
    events = []
    async def run():
        thread = await client.start_thread({'sandbox_read_only': True})
        async for event in client.run_turn(thread, 'invoke expert'):
            events.append(event)
            if event['method'] == 'ksadk/a2a/toolCall':
                assert event['params']['threadId'] == thread
                if interrupt:
                    assert await client.interrupt_active_turn(thread)
                else:
                    assert client.resolve_a2a_tool(event['params'], response)
    try:
        await asyncio.wait_for(run(), 10)
    finally:
        await asyncio.wait_for(client.close(), 5)
    completed = next(event['params']['item'] for event in events
        if event['method'] == 'item/completed' and event['params']['item']['type'] == 'dynamicToolCall')
    assert completed['success'] is (not interrupt)
    assert completed['contentItems'] == (failed_tool_result('A2A_CODEX_TOOL_CLOSED')['contentItems'] if interrupt else response['contentItems'])
    assert events[-1]['method'] == 'turn/completed'
