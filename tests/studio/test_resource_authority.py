import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from ksadk.resource_runtime.contracts import ResourceConfig
from ksadk.studio.contracts import AgentBindings, AgentSpec
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import CredentialResolver
from ksadk.studio.resource_authority import (
    ResourceAuthorityPolicy,
    SignedKnowledgeResourceAuthority,
    _HardenedSdkTransport,
    resource_authority_policy_from_environment,
)
from ksadk.studio.resource_connections import (
    ResourceConnectionDeclaration,
    ResourceConnectionRepository,
)
from ksadk.studio.workspace import Workspace
from tests.resource_runtime.test_studio_resource_config import binding


@pytest.fixture
def authority_upstream():
    state = {
        "calls": [],
        "iam_status": 200,
        "iam_listing": {
            "AccessKeyList": [
                {"AccessKey": "fixture-current-ak", "UserName": "fixture-user"}
            ]
        },
        "iam_user": {
            "GetUserResult": {
                "User": {
                    "UserId": "fixture-user-id",
                    "UserName": "fixture-user",
                    "Krn": "krn:ksc:iam::fixture-account:user/fixture-user",
                }
            }
        },
        "knowledge_status": 200,
        "knowledge": {"RequestId": "fixture-request", "Records": []},
        "knowledge_raw": None,
        "knowledge_content_length": True,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            state["calls"].append((self.path, dict(self.headers), raw))
            path = urlsplit(self.path)
            if path.path == "/iam":
                params = parse_qs(raw.decode())
                action = params.get("Action", [""])[0]
                payload = (
                    state["iam_listing"]
                    if action == "ListAllUserAccessKeys"
                    else state["iam_user"]
                )
                status = state["iam_status"]
            elif path.path == "/aicp":
                payload = state["knowledge"]
                status = state["knowledge_status"]
            elif path.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/aicp")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            else:
                payload = {"Error": {"Code": "unexpected"}}
                status = 404
            content = (
                state["knowledge_raw"]
                if path.path == "/aicp" and state["knowledge_raw"] is not None
                else json.dumps(payload).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if path.path != "/aicp" or state["knowledge_content_length"]:
                self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        yield origin, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _setup(tmp_path, origin, *, data_path="/aicp"):
    resolver = CredentialResolver()
    resolver.put_session("RESOURCE_AK", "fixture-current-ak")
    resolver.put_session("RESOURCE_SK", "fixture-current-sk")
    connections = ResourceConnectionRepository(Workspace(tmp_path), resolver)
    declaration = ResourceConnectionDeclaration.model_validate(
        {
            "label": "Fixture knowledge",
            "target": {
                "connectionRef": "connection-a",
                "tenantRef": "fixture-account",
                "principalRef": "fixture-user-id",
                "endpoint": origin + data_path,
                "authMode": "signed",
            },
            "credentials": {
                "accessKeyRef": "env://RESOURCE_AK",
                "secretKeyRef": "env://RESOURCE_SK",
            },
        }
    )
    connections.save(declaration, expected_revision=0)
    policy = ResourceAuthorityPolicy(
        iam_endpoint=origin + "/iam",
        allowed_data_endpoints=(origin + data_path,),
        allowed_regions=("region-a",),
        allow_loopback_http_for_tests=True,
    )
    config = ResourceConfig.model_validate(
        {
            "binding": {
                "id": "binding-a",
                "connectionRef": "connection-a",
                "resource": {
                    "kind": "knowledge-base",
                    "id": "dataset-a",
                    "region": "region-a",
                },
            }
        }
    )
    return resolver, connections, SignedKnowledgeResourceAuthority(connections, policy), config


def test_real_signed_transport_proves_exact_subaccount_and_knowledge_read(
    tmp_path, authority_upstream, monkeypatch
):
    origin, state = authority_upstream
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("KSYUN_IAM_URL", "http://127.0.0.1:1")
    _, _, authority, config = _setup(tmp_path, origin)

    grant = authority.admit(config, expected_connection_revision=1)

    assert grant.tenant_ref == "fixture-account"
    assert grant.resource_principal_ref == "fixture-user-id"
    assert grant.resource == config.binding.resource
    assert grant.allowed_operations == ("search_knowledge_base",)
    assert grant.request_id == "fixture-request"
    assert grant.digest.startswith("sha256:")
    assert "fixture-current-ak" not in grant.model_dump_json()
    assert "fixture-current-sk" not in grant.model_dump_json()
    assert [urlsplit(item[0]).path for item in state["calls"]] == [
        "/iam", "/iam", "/aicp"
    ]

    list_form = parse_qs(state["calls"][0][2].decode())
    get_form = parse_qs(state["calls"][1][2].decode())
    knowledge_query = parse_qs(urlsplit(state["calls"][2][0]).query)
    knowledge_body = json.loads(state["calls"][2][2])
    assert list_form["Action"] == ["ListAllUserAccessKeys"]
    assert get_form["Action"] == ["GetUser"] and get_form["UserName"] == ["fixture-user"]
    assert knowledge_query == {"Action": ["RetrieveKnowledge"], "Version": ["2025-11-14"]}
    assert knowledge_body["DatasetId"] == "dataset-a"
    assert knowledge_body["Query"] == "ksadk-resource-authority-probe"
    for _, headers, _ in state["calls"]:
        authorization = next(
            value for key, value in headers.items() if key.lower() == "authorization"
        )
        assert "fixture-current-ak/" in authorization
    iam_authorization = next(
        value
        for key, value in state["calls"][0][1].items()
        if key.lower() == "authorization"
    )
    data_authorization = next(
        value
        for key, value in state["calls"][2][1].items()
        if key.lower() == "authorization"
    )
    assert "/cn-beijing-6/iam/aws4_request" in iam_authorization
    assert "/region-a/aicp/aws4_request" in data_authorization


@pytest.mark.parametrize(
    "listing",
    [
        {},
        {"AccessKeyList": [], "AccessKeys": []},
        {
            "Code": 403,
            "AccessKeyList": [
                {"AccessKey": "fixture-current-ak", "UserName": "fixture-user"}
            ],
        },
        {
            "ResponseMetadata": {"Error": {"Code": "denied"}},
            "AccessKeyList": [
                {"AccessKey": "fixture-current-ak", "UserName": "fixture-user"}
            ],
        },
        {
            "AccessKeyList": [
                {"AccessKey": "fixture-current-ak", "UserName": "fixture-user"}
            ],
            "IsTruncated": True,
        },
        {"AccessKeyList": [
            {"AccessKey": "fixture-current-ak", "UserName": "fixture-user"},
            {"AccessKey": "fixture-current-ak", "UserName": "other-user"},
        ]},
    ],
)
def test_iam_listing_must_uniquely_prove_current_subaccount(
    tmp_path, authority_upstream, listing
):
    origin, state = authority_upstream
    state["iam_listing"] = listing
    _, _, authority, config = _setup(tmp_path, origin)
    with pytest.raises(StudioError) as rejected:
        authority.admit(config)
    assert rejected.value.code == "RESOURCE_IDENTITY_UNVERIFIED"
    assert [urlsplit(item[0]).path for item in state["calls"]] == ["/iam"]


def test_declared_identity_is_only_a_comparison(tmp_path, authority_upstream):
    origin, state = authority_upstream
    _, connections, authority, config = _setup(tmp_path, origin)
    current = connections.get("connection-a")
    changed = current.model_dump(exclude={"revision"})
    changed["target"]["principal_ref"] = "browser-claimed-user"
    connections.save(
        ResourceConnectionDeclaration.model_validate(changed), expected_revision=1
    )
    with pytest.raises(StudioError) as rejected:
        authority.admit(config)
    assert rejected.value.code == "RESOURCE_IDENTITY_MISMATCH"
    assert [urlsplit(item[0]).path for item in state["calls"]] == ["/iam", "/iam"]


@pytest.mark.parametrize("response", [{}, {"Code": 200}, {"Records": None}])
def test_incomplete_knowledge_response_never_issues_grant(
    tmp_path, authority_upstream, response
):
    origin, state = authority_upstream
    state["knowledge"] = response
    _, _, authority, config = _setup(tmp_path, origin)
    with pytest.raises(StudioError) as rejected:
        authority.admit(config)
    assert rejected.value.code == "RESOURCE_OPERATION_UNVERIFIED"


def test_redirect_is_not_followed(tmp_path, authority_upstream):
    origin, state = authority_upstream
    _, _, authority, config = _setup(tmp_path, origin, data_path="/redirect")
    with pytest.raises(StudioError) as rejected:
        authority.admit(config)
    assert rejected.value.code == "RESOURCE_OPERATION_UNVERIFIED"
    assert [urlsplit(item[0]).path for item in state["calls"]] == [
        "/iam", "/iam", "/redirect"
    ]


def test_oversized_stream_is_stopped_and_response_is_closed(
    authority_upstream, monkeypatch
):
    origin, state = authority_upstream
    state["knowledge_raw"] = b"{" + (b" " * (1024 * 1024)) + b"}"
    state["knowledge_content_length"] = False
    responses = []
    original_request = requests.Session.request

    def capture_response(session, *args, **kwargs):
        response = original_request(session, *args, **kwargs)
        responses.append(response)
        return response

    monkeypatch.setattr(requests.Session, "request", capture_response)
    request = SimpleNamespace(
        method="POST",
        uri="/",
        uri_params="",
        data=b"{}",
        header={},
        auth=None,
    )

    with pytest.raises(RuntimeError, match="RESOURCE_AUTHORITY_RESPONSE_TOO_LARGE"):
        _HardenedSdkTransport(origin + "/aicp", timeout=2).send_request(request)

    assert len(responses) == 1
    assert responses[0].raw.closed


def test_unapproved_target_is_rejected_before_secret_or_network(
    tmp_path, authority_upstream, monkeypatch
):
    origin, state = authority_upstream
    _, connections, _, config = _setup(tmp_path, origin)
    authority = SignedKnowledgeResourceAuthority(
        connections,
        ResourceAuthorityPolicy(
            iam_endpoint=origin + "/iam",
            allowed_data_endpoints=(origin + "/other",),
            allowed_regions=("region-a",),
            allow_loopback_http_for_tests=True,
        ),
    )
    monkeypatch.setattr(
        connections,
        "resolve_credentials",
        lambda *_: (_ for _ in ()).throw(AssertionError("secrets must not be read")),
    )
    with pytest.raises(StudioError) as rejected:
        authority.admit(config)
    assert rejected.value.code == "RESOURCE_AUTHORITY_TARGET_FORBIDDEN"
    assert state["calls"] == []


def test_credential_rotation_during_admission_rejects_stale_result(
    tmp_path, authority_upstream
):
    origin, _ = authority_upstream
    resolver, connections, _, config = _setup(tmp_path, origin)

    class RotatingAuthority(SignedKnowledgeResourceAuthority):
        def _prove_knowledge_read(self, config, data_endpoint, access_key, secret_key):
            resolver.put_session("RESOURCE_SK", "fixture-rotated-sk")
            return "fixture-request"

    authority = RotatingAuthority(
        connections,
        ResourceAuthorityPolicy(
            iam_endpoint=origin + "/iam",
            allowed_data_endpoints=(origin + "/aicp",),
            allowed_regions=("region-a",),
            allow_loopback_http_for_tests=True,
        ),
    )
    with pytest.raises(StudioError) as rejected:
        authority.admit(config)
    assert rejected.value.code == "RESOURCE_CONNECTION_CHANGED"


def test_non_loopback_authority_http_is_never_permitted():
    with pytest.raises(ValueError, match="HTTPS"):
        ResourceAuthorityPolicy(
            iam_endpoint="http://iam.example.test",
            allowed_data_endpoints=("https://aicp.example.test",),
            allowed_regions=("region-a",),
            allow_loopback_http_for_tests=True,
        )


def test_internal_http_requires_explicit_host_policy_and_exact_ksyun_host():
    with pytest.raises(ValueError, match="HTTPS"):
        ResourceAuthorityPolicy(
            iam_endpoint="http://iam.inner.api.ksyun.com",
            allowed_data_endpoints=("http://aicp.inner.api.ksyun.com",),
            allowed_regions=("region-a",),
        )
    with pytest.raises(ValueError, match="HTTPS"):
        ResourceAuthorityPolicy(
            iam_endpoint="http://iam.inner.api.ksyun.com.attacker.example",
            allowed_data_endpoints=("http://aicp.inner.api.ksyun.com",),
            allowed_regions=("region-a",),
            allow_ksyun_internal_http=True,
        )
    with pytest.raises(ValueError, match="service root"):
        ResourceAuthorityPolicy(
            iam_endpoint="http://iam.inner.api.ksyun.com:8080",
            allowed_data_endpoints=("http://aicp.inner.api.ksyun.com",),
            allowed_regions=("region-a",),
            allow_ksyun_internal_http=True,
        )
    with pytest.raises(ValueError, match="service root"):
        ResourceAuthorityPolicy(
            iam_endpoint="http://iam.inner.api.ksyun.com/alternate",
            allowed_data_endpoints=("http://aicp.inner.api.ksyun.com",),
            allowed_regions=("region-a",),
            allow_ksyun_internal_http=True,
        )

    policy = ResourceAuthorityPolicy(
        iam_endpoint="http://IAM.INNER.API.KSYUN.COM/",
        allowed_data_endpoints=("http://AICP.INNER.API.KSYUN.COM/",),
        allowed_regions=("region-a",),
        allow_ksyun_internal_http=True,
    )

    assert policy.iam_endpoint == "http://iam.inner.api.ksyun.com"
    assert policy.allowed_data_endpoints == ("http://aicp.inner.api.ksyun.com",)

    secure_policy = ResourceAuthorityPolicy(
        iam_endpoint="https://IAM.INNER.API.KSYUN.COM:443/",
        allowed_data_endpoints=("https://AICP.INNER.API.KSYUN.COM:443/",),
        allowed_regions=("region-a",),
        allow_ksyun_internal_http=True,
    )
    assert secure_policy.iam_endpoint == "https://iam.inner.api.ksyun.com"


def test_environment_policy_uses_parsed_hostname_for_internal_iam(monkeypatch):
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "fixture-access")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "fixture-secret")
    monkeypatch.setenv("AGENTENGINE_REGION", "cn-beijing-6")
    monkeypatch.delenv("KSADK_RESOURCE_IAM_ENDPOINT", raising=False)

    monkeypatch.setenv(
        "AGENTENGINE_SERVER_URL",
        "https://aicp.inner.api.ksyun.com.attacker.example",
    )
    public_policy = resource_authority_policy_from_environment()
    assert public_policy is not None
    assert public_policy.iam_endpoint == "https://iam.api.ksyun.com"

    monkeypatch.setenv(
        "AGENTENGINE_SERVER_URL",
        "https://aicp.inner.api.ksyun.com",
    )
    internal_policy = resource_authority_policy_from_environment()
    assert internal_policy is not None
    assert internal_policy.iam_endpoint == "http://iam.inner.api.ksyun.com"


def test_studio_validation_only_reports_verified_after_real_authority_calls(
    tmp_path, authority_upstream
):
    from ksadk.studio.service import StudioService

    origin, state = authority_upstream
    resolver = CredentialResolver()
    resolver.put_session("RESOURCE_AK", "fixture-current-ak")
    resolver.put_session("RESOURCE_SK", "fixture-current-sk")
    policy = ResourceAuthorityPolicy(
        iam_endpoint=origin + "/iam",
        allowed_data_endpoints=(origin + "/aicp",),
        allowed_regions=("region-a",),
        allow_loopback_http_for_tests=True,
    )
    studio = StudioService(
        tmp_path,
        credential_resolver=resolver,
        resource_authority_policy=policy,
    )
    studio.resource_connections.save(
        ResourceConnectionDeclaration.model_validate(
            {
                "label": "Fixture knowledge",
                "target": {
                    "connectionRef": "connection-a",
                    "tenantRef": "fixture-account",
                    "principalRef": "fixture-user-id",
                    "endpoint": origin + "/aicp",
                    "authMode": "signed",
                },
                "credentials": {
                    "accessKeyRef": "env://RESOURCE_AK",
                    "secretKeyRef": "env://RESOURCE_SK",
                },
            }
        ),
        expected_revision=0,
    )
    plugin = binding()
    plugin.config["binding"]["resource"] = {
        "kind": "knowledge-base",
        "id": "dataset-a",
        "region": "region-a",
    }
    draft = studio.drafts.create(
        agent_id="resource-agent",
        name="Resource Agent",
        spec=AgentSpec(bindings=AgentBindings(plugins=[plugin])),
    )

    result = studio.validate_resource_bindings(
        "resource-agent", expected_revision=draft.metadata.revision
    )

    assert result == {
        "revision": draft.metadata.revision,
        "valid": True,
        "authorizationVerified": True,
        "diagnostics": [],
    }
    assert len(state["calls"]) == 3
