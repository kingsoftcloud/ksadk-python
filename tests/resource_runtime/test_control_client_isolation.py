import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ksadk.api.client import AgentEngineAPIError, AgentEngineClient
from ksadk.studio.model_client import CredentialResolver
from ksadk.studio.resource_connections import ResourceConnectionRepository
from ksadk.studio.workspace import Workspace
from tests.studio.test_resource_connections import declaration


@pytest.fixture
def upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, dict(self.headers), body))
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/unexpected-target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = b'{"items": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_explicit_control_request_ignores_ambient_identity_and_transport(
    upstream, monkeypatch
):
    endpoint, calls = upstream
    for key, value in {
        "AGENTENGINE_SERVER_URL": "http://127.0.0.1:1",
        "AGENTENGINE_SIGN_SERVICE": "unrelated-service",
        "KSYUN_ACCOUNT_ID": "unrelated-account",
        "AGENTENGINE_API_VERSION": "unrelated-version",
        "AGENTENGINE_GLOBAL_DRY_RUN": "true",
        "AGENTENGINE_SSL_INSECURE": "true",
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "",
        "no_proxy": "",
        "KSYUN_ACCESS_KEY": "unrelated-access",
        "KSYUN_SECRET_KEY": "unrelated-secret",
    }.items():
        monkeypatch.setenv(key, value)
    client = AgentEngineClient(
        base_url=endpoint,
        access_key="selected-access",
        secret_key="selected-secret",
        region="selected-region",
        allow_env_fallback=False,
    )
    try:
        assert not client.dry_run
        assert client._request_ssl_verify() is True
        assert client._request_api_version() == "2024-06-12"
        assert client._get_resolved_identity() is None
        assert client._get_session().trust_env is False
        result = client._request("POST", "/fixture-resources", body={"Region": "selected-region"})
        assert result == {"items": []}
        assert len(calls) == 1
        headers = {key.lower(): value for key, value in calls[0][1].items()}
        assert "selected-access/" in headers["authorization"]
        assert "selected-region/aicp/" in headers["authorization"]
        assert "x-ksc-account-id" not in headers
        assert "x-ksc-user-uuid" not in headers
        assert client.base_url == endpoint
        assert not client._can_retry_with_inner_aicp_endpoint(
            {
                "code": "InnerAccountCanOnlyAccessThroughIntranet",
            }
        )
    finally:
        await client.close()


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": None},
        {"access_key": None},
        {"secret_key": None},
        {"region": "pre-online"},
        {"base_url": "https://user:secret@example.test"},
        {"base_url": "https://example.test:invalid"},
    ],
)
def test_explicit_client_rejects_incomplete_or_implicit_configuration(change, monkeypatch):
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "ambient-access")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "ambient-secret")
    values = {
        "base_url": "https://example.test",
        "access_key": "fake-access",
        "secret_key": "fake-secret",
        "region": "region-a",
        "allow_env_fallback": False,
    }
    with pytest.raises(ValueError):
        AgentEngineClient(**{**values, **change})


async def test_studio_connection_factory_does_not_send_declared_identity_headers(tmp_path):
    resolver = CredentialResolver()
    resolver.put_session("RESOURCE_AK", "fake-access")
    resolver.put_session("RESOURCE_SK", "fake-secret")
    repo = ResourceConnectionRepository(Workspace(tmp_path), resolver)
    saved = repo.save(declaration(), expected_revision=0)
    client = repo.create_control_client(saved.target, region="region-a")
    try:
        assert client._auth.access_key_id == "fake-access"
        assert not client._allow_env_fallback
        headers = client._build_headers()
        assert "declared-account" not in str(headers)
        assert "declared-tenant" not in str(headers)
    finally:
        await client.close()


def test_legacy_client_keeps_environment_defaults(monkeypatch):
    monkeypatch.setenv("AGENTENGINE_SERVER_URL", "https://legacy.example.test")
    monkeypatch.setenv("AGENTENGINE_SIGN_SERVICE", "legacy-service")
    monkeypatch.setenv("AGENTENGINE_API_VERSION", "legacy-version")
    client = AgentEngineClient()
    assert client.base_url == "https://legacy.example.test"
    assert client.service == "legacy-service"
    assert client._request_api_version() == "legacy-version"


async def test_explicit_control_request_never_follows_redirect(upstream):
    endpoint, calls = upstream
    client = AgentEngineClient(
        base_url=endpoint,
        access_key="fake-access",
        secret_key="fake-secret",
        region="region-a",
        allow_env_fallback=False,
    )
    try:
        with pytest.raises(AgentEngineAPIError, match="redirect refused"):
            client._request("POST", "/redirect", body={"fixture": True})
        assert [path for path, _, _ in calls] == ["/redirect"]
    finally:
        await client.close()
