"""Fake AK/SK exercise the actual signer, without contacting KOP or STS."""

import httpx
import pytest
import requests

from ksadk.skills.models import SkillRef
from ksadk.skills.request_connection import (
    CONNECTION_FIELDS,
    RequestSkillConnection,
    current_skill_connection,
    skill_connection_scope,
)
from ksadk.skills.service_client import SkillServiceClient

CREDS = {
    "access_key": "fake-ak-canary",
    "secret_key": "fake-sk-canary",
    "security_token": "fake-sts-canary",
}


@pytest.mark.parametrize("traffic_source, expected_source", [(None, "agentkit"), ("worker-source", "worker-source")])
def test_kop_mock_signs_and_download_does_not_receive_credentials(monkeypatch, traffic_source, expected_source):
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", "ambient-source")
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "host-ak-canary")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "host-sk-canary")
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.host == "download.invalid":
            assert not {
                "authorization",
                "x-ksc-security-token",
                "x-ksc-account-id",
                "x-amz-date",
            }.intersection(request.headers)
            return httpx.Response(200, content=b"archive")
        auth = request.headers["Authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=fake-ak-canary/")
        assert request.headers["X-Traffic-Source"] == expected_source
        assert request.headers["X-Test"] == "kept"
        assert "/cn-beijing-6/aicp/aws4_request" in auth
        assert request.headers["X-Ksc-Security-Token"] == CREDS["security_token"]
        assert "x-ksc-security-token" not in auth.lower()
        assert request.headers["X-KSC-CUSTOM-SOURCE"] == "pre"
        assert CREDS["secret_key"] not in str(request.headers)
        assert "host-ak-canary" not in str(request.headers)
        return httpx.Response(
            200, json={"Code": 0, "Data": {"DownloadUrl": "https://download.invalid/archive.zip"}}
        )

    client = SkillServiceClient(
        base_url="https://aicp.api.ksyun.com",
        region="cn-beijing-6",
        custom_source="pre",
        traffic_source=traffic_source,
        extra_headers={"X-Test": "kept", "x-traffic-source": "old"},
        allow_env_fallback=False,
        transport=httpx.MockTransport(respond),
        **CREDS,
    )
    assert (
        client.download_skill_archive(
            SkillRef(skill_id="skill", version_id="v1", version="1", name="writer")
        )
        == b"archive"
    )
    assert len(seen) == 2


def test_real_requests_transport_sends_prepared_signed_request(monkeypatch):
    seen = []

    def send(session, prepared, **kwargs):
        seen.append(prepared)
        assert session.trust_env is False
        assert kwargs["allow_redirects"] is False
        assert prepared.headers["Authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=fake-ak-canary/"
        )
        assert prepared.headers["X-Traffic-Source"] == "requests-source"
        assert prepared.headers["X-Ksc-Security-Token"] == CREDS["security_token"]
        assert "x-ksc-security-token" not in prepared.headers["Authorization"].lower()
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"Code":0,"Data":{"Items":[]}}'
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    client = SkillServiceClient(
        base_url="http://aicp.internal.api.ksyun.com",
        traffic_source="requests-source",
        region="cn-beijing-6",
        allow_env_fallback=False,
        **CREDS,
    )
    assert client.list_skill_spaces()["Code"] == 0
    assert len(seen) == 1
    client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://aicp.api.ksyun.com.attacker.invalid",
        "http://aicp.api.ksyun.com",
        "https://rest.invalid",
    ],
)
def test_explicit_signing_rejects_noncanonical_endpoint_without_echo(url):
    with pytest.raises(ValueError) as caught:
        SkillServiceClient(base_url=url, allow_env_fallback=False, **CREDS)
    assert "canonical KOP" in str(caught.value)
    assert all(value not in str(caught.value) for value in CREDS.values())


@pytest.mark.parametrize(
    "bad",
    [
        {"access_key": "fake-ak-canary"},
        {"secret_key": "fake-sk-canary"},
        {"security_token": "fake-sts-canary"},
        {**CREDS, "token": "fake-bearer-canary"},
    ],
)
def test_client_rejects_invalid_credential_combinations(bad):
    with pytest.raises(ValueError) as caught:
        SkillServiceClient(base_url="https://aicp.api.ksyun.com", allow_env_fallback=False, **bad)
    assert all(value not in str(caught.value) for value in bad.values())


@pytest.mark.parametrize("field", ["ak", "sk", "token", "credentials"])
def test_boundary_rejects_aliases_safely(field):
    with pytest.raises(ValueError) as caught:
        with skill_connection_scope({"skill_source": {field: "fake-secret-canary"}}):
            pass
    assert "fake-secret-canary" not in str(caught.value)


def test_boundary_strips_signing_fields_and_all_fields_affect_fingerprint():
    raw = {
        "space_id": "space",
        "service_url": "https://aicp.api.ksyun.com",
        "account_id": "fake-account",
        "region": "cn-beijing-6",
        "custom_source": "pre",
        **CREDS,
    }
    with skill_connection_scope({"agentengine": {"skill_source": raw}}) as (metadata, _):
        connection = current_skill_connection()
        assert metadata == {"agentengine": {"skill_source": {"space_id": "space"}}}
        assert all(value not in repr(connection) for value in CREDS.values())
        for field in CONNECTION_FIELDS:
            changed = RequestSkillConnection(
                {**connection.values, field: str(connection.values.get(field, "")) + "changed"}
            )
            assert connection.fingerprint != changed.fingerprint
    assert current_skill_connection() is None


@pytest.mark.parametrize("failure", ["business", "http"])
def test_signed_errors_redact_known_credentials(failure):
    def respond(request):
        echoed = " ".join(CREDS.values())
        return httpx.Response(
            200 if failure == "business" else 403,
            json={"Code": 403, "RequestId": echoed, "Message": echoed},
        )

    client = SkillServiceClient(
        base_url="https://aicp.api.ksyun.com",
        allow_env_fallback=False,
        region="cn-beijing-6",
        transport=httpx.MockTransport(respond),
        **CREDS,
    )
    with pytest.raises(ValueError) as caught:
        client.list_skill_spaces()
    assert all(value not in str(caught.value) for value in CREDS.values())


def test_default_constructor_rejects_mixed_credentials_and_preserves_environment_bearer(
    monkeypatch,
):
    with pytest.raises(ValueError, match="mix"):
        SkillServiceClient(
            base_url="https://aicp.api.ksyun.com",
            token="fake-bearer",
            access_key="fake-ak",
            secret_key="fake-sk",
        )
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "host-ak")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "host-sk")
    seen = []
    client = SkillServiceClient(
        base_url="https://rest.invalid",
        token="fake-bearer",
        transport=httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(200, json={"Code": 0})
        ),
    )
    client.list_skill_spaces()
    assert seen[0].headers["Authorization"] == "Bearer fake-bearer"
    assert "host-ak" not in str(seen[0].headers)


@pytest.mark.parametrize("base_url", ["https://rest.invalid", "https://aicp.api.ksyun.com"])
def test_bearer_transport_errors_do_not_echo_credentials(base_url):
    def fail(request):
        raise httpx.ConnectError("fake-bearer-canary", request=request)

    client = SkillServiceClient(
        base_url=base_url,
        token="fake-bearer-canary",
        allow_env_fallback=False,
        transport=httpx.MockTransport(fail),
    )
    with pytest.raises(ValueError) as caught:
        client.list_skill_spaces()
    assert "fake-bearer-canary" not in str(caught.value)
