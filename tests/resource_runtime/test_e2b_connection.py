import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ksadk.sandbox.backends.e2b import E2BSandboxBackend
from ksadk.sandbox.base import SandboxError, SandboxSpec
from ksadk.sandbox.e2b_connection import ExplicitE2BConnection
from ksadk.skills.runtime.backends.e2b import E2BSkillRuntimeBackend


def connection():
    return ExplicitE2BConnection(
        api_url="https://api.example.test", domain="example.test", api_key="fake-selected-key"
    )


def clean_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("E2B_") or key.lower().endswith("_proxy") or key.startswith("SSL_CERT_"):
            monkeypatch.delenv(key)


def test_explicit_credentials_stay_out_of_sandbox_env(monkeypatch):
    clean_environment(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "fake-other-key")
    calls = []

    class Sandbox:
        @classmethod
        def create(cls, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(files=SimpleNamespace(write_files=lambda files: None))

    backend = E2BSandboxBackend(
        spec=SandboxSpec(template_id="fixture"), sandbox_cls=Sandbox, connection=connection()
    )
    monkeypatch.setattr(backend, "_wait_until_ready", lambda *args: None)
    backend.create_session(session_id="operation")
    assert calls[0]["api_key"] == "fake-selected-key"
    assert calls[0]["api_url"] == "https://api.example.test"
    assert "fake-selected-key" not in str(calls[0]["envs"])
    assert "fake-selected-key" not in repr(connection())
    assert "fake-selected-key" not in connection().model_dump_json()
    assert (
        E2BSkillRuntimeBackend(
            template_id="fixture", connection=connection()
        ).sandbox_backend.connection
        == connection()
    )


@pytest.mark.parametrize(
    "variable", ["E2B_DEBUG", "E2B_SANDBOX_URL", "E2B_ACCESS_TOKEN", "HTTPS_PROXY"]
)
def test_ambient_connection_override_rejected(monkeypatch, variable):
    clean_environment(monkeypatch)
    monkeypatch.setenv(variable, "fake-unrelated")
    with pytest.raises(SandboxError, match="isolated worker"):
        connection().sdk_options()


def test_created_sandbox_is_killed_when_initialization_fails(monkeypatch):
    killed = []
    sandbox = SimpleNamespace(kill=lambda: killed.append(True))
    backend = E2BSandboxBackend(
        spec=SandboxSpec(template_id="fixture"),
        sandbox_cls=SimpleNamespace(create=lambda **kwargs: sandbox),
    )

    def fail(*args):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(backend, "_wait_until_ready", fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        backend.create_session(session_id="operation")
    assert killed == [True]


def test_real_pinned_sdk_accepts_connection_options(monkeypatch):
    site = os.environ.get("KSADK_TEST_E2B_SITE_PACKAGES")
    if not site:
        pytest.skip("Pinned e2b 2.15.3 inspection installation required")
    clean_environment(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "fake-unrelated-key")
    monkeypatch.setenv("E2B_API_URL", "https://other.example.test")
    monkeypatch.setenv("E2B_DOMAIN", "other.example.test")
    code = """
import json, sys
sys.path.insert(0, sys.argv[1])
from e2b.connection_config import ConnectionConfig
from importlib.metadata import version
options = json.loads(sys.stdin.read())
config = ConnectionConfig(**options)
assert config.api_key == options['api_key']
assert config.api_url == options['api_url']
assert config.domain == options['domain']
assert config.debug is False
assert config.access_token is None
assert config._sandbox_url is None
assert version('e2b') == '2.15.3'
print('verified')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, site],
        input=json.dumps(connection().sdk_options()),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "verified"
