"""Keep the pinned SDK reader live while a managed expert waits for input."""
from __future__ import annotations

import threading


def install_expert_request_reader(sync_client) -> None:
    """Install before transport startup, only for fixed-expert clients.

    openai-codex 0.154 dispatches server requests synchronously on its sole
    reader. A waiting expert therefore also blocks unrelated RPC responses and
    notifications. Writes remain serialized by the SDK's existing write lock;
    request ownership, validation and completion remain in its handler.
    """
    if getattr(sync_client, '_reader_thread', None) is not None:
        raise ValueError('A2A_CODEX_EXPERT_READER_ALREADY_STARTED')
    required = ('_read_message', '_write_message', '_handle_server_request',
                '_coerce_notification', '_router')
    if any(not hasattr(sync_client, name) for name in required):
        raise ValueError('A2A_CODEX_EXPERT_READER_UNSUPPORTED')

    def respond(message):
        try:
            result = sync_client._handle_server_request(message)
            sync_client._write_message({'id': message['id'], 'result': result})
        except BaseException as exc:
            sync_client._router.fail_all(exc)

    def read():
        try:
            while True:
                message = sync_client._read_message()
                if 'method' in message and 'id' in message:
                    # close() releases all pending bridge/approval waiters before
                    # closing the transport. Daemon workers never own a process.
                    threading.Thread(target=respond, args=(message,),
                                     name='codex-expert-request', daemon=True).start()
                elif 'method' in message:
                    method = message['method']
                    if isinstance(method, str):
                        sync_client._router.route_notification(
                            sync_client._coerce_notification(method, message.get('params')))
                else:
                    sync_client._router.route_response(message)
        except BaseException as exc:
            sync_client._router.fail_all(exc)

    sync_client._reader_loop = read
