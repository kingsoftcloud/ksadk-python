"""Verify source attribution at HTTP boundaries, including real upstream SDKs."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from ksadk.common.cloud_headers import build_cloud_headers, resolve_traffic_source


@pytest.mark.parametrize("value", [None, "", "   ", "studio-fixture"])
def test_source_configuration_and_settings(monkeypatch, value):
    from ksadk.configs import settings
    from ksadk.deployment.env_forward import should_forward_process_env

    if value is None:
        monkeypatch.delenv("KSADK_TRAFFIC_SOURCE", raising=False)
    else:
        monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", value)
    expected = "studio-fixture" if value == "studio-fixture" else "agentkit"
    assert resolve_traffic_source() == expected
    assert settings.cloud.traffic_source == expected
    assert build_cloud_headers()["X-Traffic-Source"] == expected
    assert should_forward_process_env("KSADK_TRAFFIC_SOURCE")


@pytest.mark.parametrize("value", ["x\r\nX-Other: bad", "x\n", "\t", "x\x7f", "来源"])
def test_source_rejects_invalid_header_values(monkeypatch, value):
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", value)
    with pytest.raises(ValueError, match="printable ASCII"):
        build_cloud_headers()


def test_source_merge_preserves_auth_and_does_not_mutate_input(monkeypatch):
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", "host-source")
    original = {
        "Authorization": "Bearer fixture",
        "x-traffic-source": "old",
        "X-Traffic-Source": "old2",
    }
    result = build_cloud_headers(original, traffic_source="worker-source")
    assert result == {"Authorization": "Bearer fixture", "X-Traffic-Source": "worker-source"}
    assert original["x-traffic-source"] == "old"


@pytest.fixture
def cloud_upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self._reply()

        def do_DELETE(self):
            self._reply()

        def _reply(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append(
                (self.command, self.path, dict(self.headers), json.loads(body) if body else {})
            )
            # E2B manager schema plus a valid generic AICP/REST envelope.
            result = json.dumps(
                {
                    "Code": 0,
                    "Data": {},
                    "RequestId": "fixture-request",
                    "clientID": "client-fixture",
                    "sandboxID": "sandbox-fixture",
                    "templateID": "template-fixture",
                    "envdVersion": "0.2.6",
                    "domain": "example.test",
                }
            ).encode()
            self.send_response(
                204 if self.command == "DELETE" else 201 if self.path == "/sandboxes" else 200
            )
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Content-Length", "0" if self.command == "DELETE" else str(len(result))
            )
            self.end_headers()
            if self.command != "DELETE":
                self.wfile.write(result)

        def log_message(self, *_args):
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


@pytest.mark.parametrize("source", ["agentkit", "studio-fixture"])
@pytest.mark.parametrize("kind", ["knowledge", "memory"])
def test_aicp_sdk_emits_source_before_signing(cloud_upstream, monkeypatch, source, kind):
    from ksadk.knowledge_base.client import KnowledgeBaseClient
    from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend

    endpoint, calls = cloud_upstream
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", source)
    kwargs = dict(
        endpoint=endpoint.removeprefix("http://"),
        scheme="http",
        access_key="fake-access",
        secret_key="fake-secret",
    )
    if kind == "knowledge":
        owner = KnowledgeBaseClient(dataset_id="kb-fixture", **kwargs)
        actions = ["RetrieveKnowledge"]
    else:
        owner = SdkLTMBackend(index="memory-fixture", **kwargs)
        actions = [
            "CreateMemorySdk",
            "QueryMemorySdk",
            "ListMemories",
            "UpdateMemory",
            "DeleteMemory",
            "ListSessions",
        ]
    options = {"IsPostJson": True, "CustomHeaders": {"X-Test": "kept", "x-traffic-source": "old"}}
    for action in actions:
        owner._get_client().call(action, {}, options=options)
    assert options["CustomHeaders"]["x-traffic-source"] == "old"
    assert len(calls) == len(actions)
    for _, _, raw, _ in calls:
        headers = httpx.Headers(raw)
        assert headers["X-Traffic-Source"] == source
        assert headers["X-Test"] == "kept"
        assert "fake-access/" in headers["Authorization"]
        assert headers["Content-Type"].split(";")[0] == "application/json"


@pytest.mark.parametrize("source", ["agentkit", "studio-fixture"])
def test_e2b_sdk_create_reconnect_and_cleanup_emit_source(cloud_upstream, monkeypatch, source):
    from e2b import Sandbox

    from ksadk.sandbox import E2BSandboxBackend, SandboxSpec

    endpoint, calls = cloud_upstream
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", source)
    monkeypatch.setenv("E2B_API_URL", endpoint)
    monkeypatch.setenv("E2B_API_KEY", "fake-e2b-key")
    for key in ("E2B_DEBUG", "E2B_SANDBOX_URL", "E2B_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    backend = E2BSandboxBackend(
        spec=SandboxSpec(template_id="template-fixture"), sandbox_cls=Sandbox
    )
    monkeypatch.setattr(backend, "_wait_until_ready", lambda *_args: None)
    session = backend.create_session(session_id="session-fixture")
    # No envd process is running in this HTTP fixture; retain the real manager
    # transport while bypassing only its post-connect readiness command.
    monkeypatch.setattr(
        "ksadk.sandbox.backends.e2b._with_startup_retry", lambda *_args, **_kwargs: None
    )
    reconnected = backend.reconnect_session(session_locator=session.sandbox_id)
    assert reconnected._sandbox.connection_config.sandbox_headers["X-Traffic-Source"] == source
    session.kill()
    assert [(method, path) for method, path, _, _ in calls] == [
        ("POST", "/sandboxes"),
        ("POST", "/sandboxes/sandbox-fixture/connect"),
        ("DELETE", "/sandboxes/sandbox-fixture"),
    ]
    assert all(httpx.Headers(headers)["X-Traffic-Source"] == source for _, _, headers, _ in calls)


def test_control_clients_emit_source(cloud_upstream, monkeypatch):
    from ksadk.api.client import AgentEngineClient
    from ksadk.common.kop_client import KOPClient
    from ksadk.skills.service_client import SkillServiceClient

    endpoint, calls = cloud_upstream
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", "control-fixture")
    client = AgentEngineClient(
        region="cn-beijing-6", dry_run=True, extra_headers={"x-traffic-source": "old"}
    )
    monkeypatch.setattr(client, "_resolve_user_uuid", lambda: None)
    monkeypatch.setattr(client, "_resolve_account_id", lambda: None)
    headers = client._build_headers()
    assert headers["X-Traffic-Source"] == "control-fixture"
    assert "x-traffic-source" not in headers
    assert headers["X-Ksc-Source"] == "ksadk-cli"
    kop = KOPClient(base_url=endpoint)
    try:
        kop.post_action("ListAToASpaceAgents")
    finally:
        kop._session_obj().close()
    assert httpx.Headers(calls[0][2])["X-Traffic-Source"] == "control-fixture"
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"Code": 0, "Data": {}})

    skills = SkillServiceClient(
        base_url="https://skills.example.test", transport=httpx.MockTransport(handler)
    )
    skills.list_skill_spaces()
    assert seen[0].headers["X-Traffic-Source"] == "control-fixture"


def test_global_cloud_config_roundtrip(monkeypatch):
    from ksadk.configs import global_config

    monkeypatch.setattr(global_config, "load_global_config", lambda: {})
    config = global_config.build_global_config_from_env({"KSADK_TRAFFIC_SOURCE": "studio-fixture"})
    assert config["cloud"]["KSADK_TRAFFIC_SOURCE"] == "studio-fixture"
    monkeypatch.setattr(global_config, "load_global_config", lambda: config)
    assert global_config.get_env_from_global_config()["KSADK_TRAFFIC_SOURCE"] == "studio-fixture"


def test_http_memory_client_emits_source(cloud_upstream, monkeypatch):
    from ksadk.memory.adk.backends.http_ltm_backend import HttpLTMBackend

    endpoint, calls = cloud_upstream
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", "http-memory-fixture")
    backend = HttpLTMBackend(index="fixture", base_url=endpoint, token="fake-token")
    try:
        backend.client.post("/memories", json={"events": ["fixture"]})
    finally:
        backend.client.close()
    headers = httpx.Headers(calls[0][2])
    assert headers["X-Traffic-Source"] == "http-memory-fixture"
    assert headers["Authorization"] == "Bearer fake-token"


def test_explicit_skill_source_is_isolated_from_ambient_source(monkeypatch):
    from ksadk.skills.service_client import SkillServiceClient

    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", "unrelated-host-source")
    client = SkillServiceClient(
        base_url="https://skills.example.test",
        region="cn-beijing-6",
        allow_env_fallback=False,
        traffic_source="worker-source",
    )
    assert client._headers()["X-Traffic-Source"] == "worker-source"
    default = SkillServiceClient(
        base_url="https://skills.example.test", region="cn-beijing-6", allow_env_fallback=False
    )
    assert default._headers()["X-Traffic-Source"] == "agentkit"
