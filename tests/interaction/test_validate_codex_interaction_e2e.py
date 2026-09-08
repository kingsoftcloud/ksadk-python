from __future__ import annotations

import importlib.util
from pathlib import Path

import requests

SCRIPT = Path(__file__).parents[2] / "scripts" / "validate_codex_interaction_e2e.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_codex_interaction_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SseResponse:
    def __init__(self) -> None:
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, **_kwargs):
        yield 'id: 90'
        yield (
            'data: {"seq":90,"family":"interaction",'
            '"event_type":"interaction.requested","interaction_id":"ix-1",'
            '"run_id":"run-1","revision":1}'
        )
        yield 'id: 95'
        yield (
            'data: {"seq":95,"family":"interaction",'
            '"event_type":"interaction.resolved","interaction_id":"ix-1",'
            '"run_id":"run-1","revision":2}'
        )
        yield 'id: 171'
        yield (
            'data: {"seq":171,"family":"runtime",'
            '"event_type":"run.completed","run_id":"run-1",'
            '"status":"completed"}'
        )
        raise requests.exceptions.ConnectionError("bounded SSE read timeout")

    def close(self) -> None:
        self.closed = True


def test_gate_reads_authoritative_interaction_and_terminal_sse(monkeypatch):
    validator = _load_validator()
    response = _SseResponse()
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(validator.requests, "get", fake_get)
    events = validator._read_runtime_events(
        endpoint="http://runtime.example",
        api_key="secret-not-logged",
        agent_id="agent-1",
        session_id="session-1",
    )

    requested = validator._interaction_request(events[0])
    resolved = validator._interaction_response(events[1])
    assert requested == {
        "interaction_id": "ix-1",
        "run_id": "run-1",
        "revision": 1,
        "event_id": None,
        "seq_id": 90,
        "projection": "canonical",
    }
    assert resolved is not None and resolved["seq_id"] == 95
    assert validator._terminal_after(events, resolved["seq_id"]) is True
    assert calls[0][0].endswith("/agentengine/api/v1/SubscribeSessionEvents")
    assert calls[0][1]["params"]["SessionId"] == "session-1"
    assert response.closed is True
