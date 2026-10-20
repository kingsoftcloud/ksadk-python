import asyncio
import math

import pytest

from ksadk.server import history_deadline as module


@pytest.mark.parametrize('value', ['', '0', '-1', 'nan', 'inf', '-inf', 'oops'])
def test_invalid_budget_keeps_finite_default(monkeypatch, value):
    monkeypatch.setenv('KSADK_HISTORY_REQUEST_SECONDS', value)
    assert module.HistoryDeadlineMiddleware(None).budget_seconds == 15.0


def test_budget_alias_and_precedence(monkeypatch):
    monkeypatch.delenv('KSADK_HISTORY_REQUEST_SECONDS', raising=False)
    monkeypatch.setenv('BUDGET_TIMEOUT', '60')
    assert module.HistoryDeadlineMiddleware(None).budget_seconds == 60.0
    monkeypatch.setenv('KSADK_HISTORY_REQUEST_SECONDS', '12.5')
    assert module.HistoryDeadlineMiddleware(None).budget_seconds == 12.5
    assert math.isinf(module._remaining_ms('9' * 10000))


@pytest.mark.parametrize('trusted,carrier,action,expected', [
    (True, '1', 'ListSessionMessages', 504),
    (True, '9' * 100, 'ListSessionMessages', 200),
    (False, '1', 'ListSessionMessages', 200),
    (True, '1', 'RunAgent', 200),
])
async def test_configured_budget_retains_trusted_shortening_and_run_exclusion(
    monkeypatch, trusted, carrier, action, expected,
):
    monkeypatch.setenv('KSADK_HISTORY_REQUEST_SECONDS', '.1')
    monkeypatch.setattr(module, 'trusted_invocation_identity_from_headers',
                        lambda _: type('Identity', (), {'is_empty': not trusted})())
    messages = []
    async def app(scope, receive, send):
        await asyncio.sleep(.02)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'{}'})
    async def send(message):
        messages.append(message)
    await module.HistoryDeadlineMiddleware(app)(
        {'type': 'http', 'method': 'POST', 'path': '/api/' + action,
         'headers': [(b'x-agentengine-remaining-ms', carrier.encode())]}, None, send,
    )
    assert messages[0]['status'] == expected


async def test_huge_remaining_carrier_cannot_extend_configured_deadline(monkeypatch):
    monkeypatch.setenv("KSADK_HISTORY_REQUEST_SECONDS", ".01")
    monkeypatch.setattr(
        module, "trusted_invocation_identity_from_headers",
        lambda _: type("Identity", (), {"is_empty": False})(),
    )
    messages = []

    async def app(scope, receive, send):
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(module.HistoryDeadlineMiddleware(app)(
        {"type": "http", "method": "POST", "path": "/api/ListSessionMessages",
         "headers": [(b"x-agentengine-remaining-ms", b"9" * 10000)]}, None, send,
    ), timeout=1)
    assert [message["type"] for message in messages] == [
        "http.response.start", "http.response.body",
    ]
    assert messages[0]["status"] == 504
    assert b"RUNTIME_HISTORY_TIMEOUT" in messages[1]["body"]
