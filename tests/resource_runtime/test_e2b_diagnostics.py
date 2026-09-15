import logging
import os
from types import SimpleNamespace

import pytest

from ksadk.sandbox.backends.e2b import E2BSandboxBackend, E2BSandboxSessionError
from ksadk.sandbox.base import SandboxSpec
from ksadk.sandbox.diagnostics import e2b_diagnostic_step
from ksadk.sandbox.e2b_connection import ExplicitE2BConnection


@pytest.mark.parametrize("failed_step", ["ready.command", "ready.file", "ready.env"])
def test_initialization_logs_exact_failure_and_cleanup(monkeypatch, caplog, failed_step):
    monkeypatch.setenv("KSADK_SANDBOX_STARTUP_RETRY_ATTEMPTS", "1")
    caplog.set_level(logging.INFO)
    calls = []

    def operation(step):
        calls.append(step)
        if step == failed_step:
            raise RuntimeError("connection closed")
        return SimpleNamespace(stdout="value", stderr="", exit_code=0)

    sandbox = SimpleNamespace(
        sandbox_id="diagnostic-instance",
        commands=SimpleNamespace(
            run=lambda command, **kw: operation(
                "ready.command" if command == "true" else "ready.env"
            )
        ),
        files=SimpleNamespace(write=lambda *args: operation("ready.file")),
        kill=lambda: operation("cleanup"),
    )
    backend = E2BSandboxBackend(
        spec=SandboxSpec(template_id="fixture"),
        sandbox_cls=SimpleNamespace(create=lambda **kwargs: sandbox),
    )
    with pytest.raises(E2BSandboxSessionError) as raised:
        backend.create_session(session_id="operation", env={"TEST_ENV": "value"})
    assert raised.value.failure_stage == "initialize"
    assert raised.value.cleanup_status == "completed"
    assert calls[-1] == "cleanup"
    assert f"step={failed_step} status=failed runtime_id=diagnostic-instance" in caplog.text
    assert "step=initialize.cleanup status=completed" in caplog.text
    assert "RuntimeError: connection closed" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text


def test_exception_chain_redacts_credentials_and_signed_urls(monkeypatch, caplog):
    monkeypatch.setenv("E2B_API_KEY", "fake-environment-key")
    original = RuntimeError("outer fake-explicit-key fake-runtime-secret")
    with pytest.raises(RuntimeError) as raised:
        with e2b_diagnostic_step(
            "ready.file",
            sensitive_values=("fake-explicit-key",),
            env={"CUSTOM": "fake-runtime-secret"},
        ):
            try:
                raise ValueError(
                    "fake-environment-key https://example.test/file?signature=fake-signature"
                )
            except ValueError as exc:
                raise original from exc
    assert raised.value is original
    for secret in (
        "fake-environment-key",
        "fake-explicit-key",
        "fake-runtime-secret",
        "fake-signature",
    ):
        assert secret not in caplog.text
    assert "ValueError" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "direct cause" in caplog.text


def test_failed_cleanup_keeps_original_error_and_both_steps(monkeypatch, caplog):
    for key in tuple(os.environ):
        if key.lower().endswith("_proxy") or key.startswith("E2B_") or key.startswith("SSL_CERT_"):
            monkeypatch.delenv(key)
    original = RuntimeError("initialization failed fake-explicit-key")

    def command(*args, **kwargs):
        raise original

    def kill():
        raise OSError("cleanup disconnected fake-explicit-key")

    backend = E2BSandboxBackend(
        spec=SandboxSpec(template_id="fixture"),
        sandbox_cls=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                sandbox_id="diagnostic-instance",
                commands=SimpleNamespace(run=command),
                kill=kill,
            )
        ),
        connection=ExplicitE2BConnection(
            api_url="https://example.test", domain="example.test", api_key="fake-explicit-key"
        ),
    )
    with pytest.raises(E2BSandboxSessionError) as raised:
        backend.create_session(session_id="operation")
    assert raised.value.cause is original
    assert raised.value.cleanup_status == "failed"
    assert "step=ready.command status=failed" in caplog.text
    assert "step=initialize.cleanup status=failed" in caplog.text
    assert "cleanup disconnected" in caplog.text
    assert "fake-explicit-key" not in caplog.text


@pytest.mark.parametrize("failed_step", ["reconnect", "reconnect.ready.command"])
def test_reconnect_failure_preserves_instance_and_redacts_diagnostics(
    monkeypatch, caplog, failed_step
):
    monkeypatch.setenv("KSADK_SANDBOX_STARTUP_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr(ExplicitE2BConnection, "sdk_options", lambda self: {})
    caplog.set_level(logging.INFO)
    original = RuntimeError("connection closed fake-explicit-key fake-runtime-secret")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise original

    sandbox = SimpleNamespace(
        sandbox_id="existing-instance",
        commands=SimpleNamespace(run=run),
        kill=lambda: calls.append("kill"),
    )

    def connect(locator, **kwargs):
        calls.append("connect")
        if failed_step == "reconnect":
            raise original
        return sandbox

    backend = E2BSandboxBackend(
        spec=SandboxSpec(template_id="fixture", env={"CUSTOM": "fake-runtime-secret"}),
        sandbox_cls=SimpleNamespace(connect=connect),
        connection=ExplicitE2BConnection(
            api_url="https://example.test", domain="example.test", api_key="fake-explicit-key"
        ),
    )
    with pytest.raises(RuntimeError) as raised:
        backend.reconnect_session(session_locator="existing-instance")
    assert raised.value is original
    assert "kill" not in calls
    assert f"step={failed_step} status=failed runtime_id=existing-instance" in caplog.text
    assert "fake-explicit-key" not in caplog.text
    assert "fake-runtime-secret" not in caplog.text
