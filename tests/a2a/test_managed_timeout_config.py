import pytest
from ksadk.a2a.timeouts import managed_invocation_timeouts, seconds


def test_managed_phase_caps_keep_model_silence_separate():
    budget = managed_invocation_timeouts()
    assert budget.prepare_seconds == 30 and budget.connect_seconds == 30
    assert budget.first_event_seconds == 300 and budget.idle_seconds == 300


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "invalid", "true"])
def test_invalid_timeout_uses_finite_default(monkeypatch, raw):
    monkeypatch.setenv("KSADK_A2A_NATIVE_TRIGGER_SECONDS", raw)
    assert seconds("KSADK_A2A_NATIVE_TRIGGER_SECONDS", 15) == 15


def test_explicit_managed_phase_caps(monkeypatch):
    monkeypatch.setenv("KSADK_A2A_PREPARE_SECONDS", "42.5")
    monkeypatch.setenv("KSADK_A2A_CONNECT_SECONDS", "0.025")
    budget = managed_invocation_timeouts()
    assert budget.prepare_seconds == 42.5 and budget.connect_seconds == 0.025
