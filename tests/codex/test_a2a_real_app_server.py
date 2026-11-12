"""Real app-server and local deterministic model endpoint; no real-model claim."""
import asyncio
from pathlib import Path

import pytest

from tests.a2a.test_invocation_service_r85 import request
from tests.e2e.codex_app_server_fixture import RealCodexFactory
from tests.e2e.codex_responses_stub import DeterministicResponsesStub, _message_events, _completed_event


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_start", [False, True, "close_pending"])
async def test_real_app_server_exposes_expert_and_preserves_callback_identity(tmp_path, parallel_start):
    from openai_codex import CodexConfig
    calls = []
    def responses(payload):
        outputs = [item for item in payload.get('input', []) if item.get('type') == 'function_call_output']
        if outputs:
            assert outputs[-1]['call_id'] == 'call-expert-1'
            assert 'expert reply' in str(outputs[-1]['output'])
            return _message_events('expert complete', suffix='expert-final')
        item = {'type': 'function_call', 'id': 'fc-expert-1', 'call_id': 'call-expert-1',
            'name': 'expert', 'arguments': '{"message":"hello"}'}
        return ({'type': 'response.created', 'response': {'id': 'response-expert'}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
            {'type': 'response.function_call_arguments.done', 'item_id': item['id'],
             'output_index': 0, 'arguments': item['arguments']},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            _completed_event('response-expert', [item]))
    with DeterministicResponsesStub() as stub:
        stub._events = responses
        factory = RealCodexFactory(responses_url=stub.base_url)
        client = factory(CodexConfig(env={'CODEX_HOME': str(tmp_path / 'codex'), 'HOME': str(tmp_path)}))
        client.configure_a2a_experts([request().spec])
        events = []
        async def run():
            thread = await client.start_thread({'model': 'fixture-model', 'ephemeral': False})
            async for event in client.run_turn(thread, 'ask expert'):
                events.append(event)
                if event['method'] == 'ksadk/a2a/toolCall':
                    calls.append(event['params'])
                    assert event['params']['threadId'] == thread
                    if parallel_start == "close_pending":
                        await asyncio.wait_for(client.close(), 5)
                        return
                    if parallel_start:
                        # The first native tool callback is still awaiting its
                        # response. A different session must remain startable.
                        other = await asyncio.wait_for(client.start_thread(
                            {'model': 'fixture-model', 'ephemeral': False}), 3)
                        assert other != thread
                    client.resolve_a2a_tool(event['params'], {'success': True,
                        'contentItems': [{'type': 'inputText', 'text': 'expert reply'}]})
        try:
            await asyncio.wait_for(run(), 30)
        finally:
            await client.close()
        assert len(calls) == 1
        if parallel_start == "close_pending":
            import threading
            assert all(process.poll() is not None for process in factory.processes)
            assert not any(t.is_alive() and t.name == 'codex-expert-request'
                           for t in threading.enumerate())
            return
        native = [event['params'] for event in events if event['method'] == 'item/started'
                  and event['params']['item']['type'] == 'dynamicToolCall']
        assert len(native) == 1
        assert native[0]['item']['id'] == calls[0]['callId']
        assert native[0]['turnId'] == calls[0]['turnId']
        assert stub.requests()
        assert all(process.poll() is not None for process in factory.processes)


@pytest.mark.asyncio
async def test_real_app_server_shared_adapter_survives_completed_sessions(tmp_path):
    from openai_codex import CodexConfig
    from ksadk.codex.runtime import CodexRuntimeAdapter
    from ksadk.runtime.adapter import StartRequest
    from ksadk.server.factory import RuntimeAppConfig, _kernel_adapter_provider

    with DeterministicResponsesStub() as stub:
        stub._events = lambda payload: _message_events('shared transport alive', suffix='shared')
        factory = RealCodexFactory(responses_url=stub.base_url)
        client = factory(CodexConfig(env={'CODEX_HOME': str(tmp_path / 'codex'), 'HOME': str(tmp_path)}))
        adapter = CodexRuntimeAdapter(client)
        provider = _kernel_adapter_provider(RuntimeAppConfig(a2a_runtime_adapter=adapter))
        threads = []
        try:
            for session in ('first', 'second'):
                handle = await provider().start(StartRequest(session_id=session, user_id='fixture-user',
                    input='hello', model='fixture-model', config={'ephemeral': False}))
                threads.append(handle.native_ref['thread_id'])
                events = await asyncio.wait_for(_collect(adapter.stream(handle)), 30)
                assert events[-1].event_type == 'run.completed'
                await adapter.close(handle)
                assert all(process.poll() is None for process in factory.processes)
            assert len(set(threads)) == 2
        finally:
            await adapter.close_app_resources()
        assert all(process.poll() is not None for process in factory.processes)


async def _collect(events):
    return [event async for event in events]
