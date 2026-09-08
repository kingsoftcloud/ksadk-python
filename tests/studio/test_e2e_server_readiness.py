"""Browser readiness budgets include successful first-run DSH startup."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tests.studio.e2e import studio_e2e_support as support


@pytest.mark.parametrize("startup_seconds", [0.0, 7.28])
def test_startup_waits_for_real_health_response(monkeypatch, tmp_path, startup_seconds):
    clock, server = _fake_server(monkeypatch, startup_seconds=startup_seconds)
    with support.studio_server(tmp_path) as base_url:
        assert base_url.startswith("http://127.0.0.1:")
        assert clock[0] >= startup_seconds
        assert server.started
    assert server.should_exit


def test_startup_deadline_reports_server_state(monkeypatch, tmp_path):
    clock, server = _fake_server(monkeypatch, startup_seconds=40.0)
    with pytest.raises(AssertionError, match="30 seconds.*started=False, thread_alive=True"):
        with support.studio_server(tmp_path):
            pytest.fail("An unhealthy server cannot be yielded")
    assert 30.0 <= clock[0] < 30.1
    assert server.should_exit


def test_dead_server_thread_fails_without_waiting_for_deadline(monkeypatch, tmp_path):
    clock, server = _fake_server(monkeypatch, startup_seconds=40.0, thread_alive=False)
    with pytest.raises(AssertionError, match="thread exited"):
        with support.studio_server(tmp_path):
            pytest.fail("A dead server cannot be yielded")
    assert clock[0] == 0.0
    assert server.should_exit


def _fake_server(monkeypatch, *, startup_seconds, thread_alive=True):
    clock = [0.0]
    server = SimpleNamespace(started=False, should_exit=False, run=lambda: None)
    monkeypatch.setattr(support.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        support.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(support, "create_studio_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(support.uvicorn, "Config", lambda *args, **kwargs: None)
    monkeypatch.setattr(support.uvicorn, "Server", lambda config: server)
    monkeypatch.setattr(
        support.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(
            start=lambda: None,
            is_alive=lambda: thread_alive,
            join=lambda **kwargs: None,
        ),
    )

    def health(url, *, timeout):
        assert url.endswith("/api/v1/system/health")
        if clock[0] < startup_seconds:
            raise ConnectionRefusedError("not listening yet")
        server.started = True
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(support, "urlopen", health)
    return clock, server
