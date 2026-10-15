"""Browser readiness budgets include successful first-run DSH startup."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tests.studio.e2e import studio_e2e_support as support


@pytest.mark.parametrize("startup_seconds", [0.0, 7.28, 40.0])
def test_startup_waits_for_real_health_response(monkeypatch, tmp_path, startup_seconds):
    clock, server = _fake_server(monkeypatch, startup_seconds=startup_seconds)
    with support.studio_server(tmp_path) as base_url:
        assert base_url.startswith("http://127.0.0.1:")
        assert clock[0] >= startup_seconds
        assert server.started
    assert server.should_exit


def test_startup_deadline_reports_server_state(monkeypatch, tmp_path):
    clock, server = _fake_server(monkeypatch, startup_seconds=130.0)
    with pytest.raises(AssertionError, match="120 seconds.*started=False, thread_alive=True"):
        with support.studio_server(tmp_path):
            pytest.fail("An unhealthy server cannot be yielded")
    assert 120.0 <= clock[0] < 120.1
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
    original_thread, original_event = support.threading.Thread, support.threading.Event
    running, stopped = original_event(), original_event()
    server = SimpleNamespace(
        started=False,
        should_exit=False,
        lifespan=SimpleNamespace(startup_failed=False, shutdown_failed=False),
    )

    def run():
        running.set()
        if thread_alive:
            stopped.wait()

    server.run = run
    monkeypatch.setattr(support.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        support.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(support, "create_studio_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(support.uvicorn, "Config", lambda *args, **kwargs: None)
    monkeypatch.setattr(support.uvicorn, "Server", lambda config: server)
    class ControlledThread:
        def __init__(self, **kwargs):
            self.thread = original_thread(**kwargs)

        def start(self):
            self.thread.start()
            try:
                assert running.wait(timeout=5), "Server target did not reach run()"
                if not thread_alive:
                    self.thread.join(timeout=5)
                    assert not self.thread.is_alive(), "Dead server target did not exit"
            except BaseException:
                server.should_exit = True
                stopped.set()
                self.thread.join(timeout=5)
                raise

        def is_alive(self):
            return self.thread.is_alive()

        def join(self, **kwargs):
            try:
                assert server.should_exit, "Host must request shutdown before joining"
            finally:
                stopped.set()
                self.thread.join(**kwargs)

    monkeypatch.setattr(support.threading, "Thread", ControlledThread)

    def health(url, *, timeout):
        assert url.endswith("/api/v1/system/health")
        if clock[0] < startup_seconds:
            raise ConnectionRefusedError("not listening yet")
        server.started = True
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(support, "urlopen", health)
    return clock, server
