"""Protocol fixture for real SDK dynamic-tool transport; no model is simulated as real."""
import json
import os
import sys
import time
from pathlib import Path

from fake_app_server import THREAD_ID, TURN_ID, _write, _thread, _thread_response, _turn, _notify, _item_notification


def main():
    home = os.environ.get('CODEX_HOME')
    rollout = Path(home) / 'sessions' / f'rollout-fixture-{THREAD_ID}.jsonl' if home else None
    completed = False
    item = {'id': 'expert-call', 'type': 'dynamicToolCall', 'tool': 'expert',
            'arguments': {'message': 'hello'}, 'namespace': None, 'status': 'inProgress'}
    for line in sys.stdin:
        raw = json.loads(line)
        method = raw.get('method')
        if method == 'initialize':
            _write({'id': raw['id'], 'result': {'serverInfo': {'name': 'fixture', 'version': 'test'}, 'userAgent': 'fixture'}})
        elif method == 'thread/start':
            assert raw['params']['dynamicTools'][0]['name'] == 'expert'
            _write({'id': raw['id'], 'result': _thread_response()})
        elif method == 'turn/start':
            if rollout is not None:
                rollout.parent.mkdir(parents=True, exist_ok=True)
                rows = [
                    {'type': 'session_meta', 'payload': {'id': THREAD_ID}},
                    {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': TURN_ID}},
                    {'type': 'turn_context', 'payload': {'turn_id': TURN_ID}},
                    {'type': 'response_item', 'payload': {'type': 'function_call',
                        'call_id': item['id'], 'name': item['tool'],
                        'arguments': json.dumps(item['arguments'])}},
                ]
                rollout.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            _write({'id': raw['id'], 'result': {'turn': _turn()}})
            time.sleep(0.05)
            _notify('turn/started', {'threadId': THREAD_ID, 'turn': _turn()})
            _item_notification(item, completed=False)
            _write({'id': 'expert-request', 'method': 'item/tool/call', 'params': {
                'threadId': THREAD_ID, 'turnId': TURN_ID, 'callId': item['id'],
                'tool': item['tool'], 'arguments': item['arguments']}})
        elif method == 'thread/read':
            _write({'id': raw['id'], 'result': {'thread': {**_thread(),
                    'turns': [_turn('completed' if completed else 'inProgress')],
                    'path': str(rollout) if rollout else None}}})
        elif raw.get('id') == 'expert-request' and method is None:
            result = raw['result']
            completed = True
            if rollout is not None:
                with rollout.open('a') as target:
                    target.write(json.dumps({'type': 'event_msg', 'payload': {
                        'type': 'task_complete', 'turn_id': TURN_ID}}) + '\n')
            _item_notification({**item, 'status': 'completed', **result}, completed=True)
            _notify('turn/completed', {'threadId': THREAD_ID, 'turn': _turn('completed')})
        elif method == 'turn/interrupt':
            _write({'id': raw['id'], 'result': {}})


if __name__ == '__main__':
    main()
