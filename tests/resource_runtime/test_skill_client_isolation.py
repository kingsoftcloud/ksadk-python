import httpx
import pytest

from ksadk.skills.models import SkillRef
from ksadk.skills.service_client import SkillServiceClient


@pytest.fixture
def polluted_environment(monkeypatch):
    for key, value in {
        "KSYUN_ACCESS_KEY": "fake-other-access",
        "KSYUN_SECRET_KEY": "fake-other-secret",
        "KSYUN_ACCOUNT_ID": "other-account",
        "KSYUN_REGION": "other-region",
        "KSADK_SKILL_SERVICE_API_VERSION": "other-version",
        "KSADK_SKILL_SERVICE_SIGN_SERVICE": "other-service",
        "HTTPS_PROXY": "http://proxy.invalid:9999",
    }.items():
        monkeypatch.setenv(key, value)


def test_explicit_token_client_sends_no_ambient_identity(polluted_environment):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"Data": {"Skills": []}})

    client = SkillServiceClient(
        base_url="https://skills.example.test/api",
        token="fake-token",
        region="region-a",
        allow_env_fallback=False,
        transport=httpx.MockTransport(handle),
    )
    client.list_skills_by_space_id("space-a")
    assert requests[0].headers["Authorization"] == "Bearer fake-token"
    assert "X-Ksc-Account-Id" not in requests[0].headers
    assert client.access_key == client.secret_key == client.account_id == ""
    assert not client._auth.is_enabled
    assert client._auth.access_key_id == client._auth.secret_access_key == ""
    assert client.region == "region-a"
    assert client.api_version == "2024-06-12"
    assert client.sign_service == "aicp"
    assert client._client_kwargs()["trust_env"] is False
    assert client._requests().trust_env is False
    client.close()
    assert client._requests_session is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"region": ""},
        {"region": "pre-online"},
        {"access_key": "fake-access"},
        {"token": "fake-token", "access_key": "fake-access", "secret_key": "fake-secret"},
    ],
)
def test_explicit_mode_rejects_ambiguous_configuration(polluted_environment, kwargs):
    with pytest.raises(ValueError):
        SkillServiceClient(
            **{
                "base_url": "https://skills.example.test",
                "region": "region-a",
                "allow_env_fallback": False,
                **kwargs,
            }
        )


def test_legacy_environment_configuration_remains_available(polluted_environment):
    client = SkillServiceClient(base_url="https://skills.example.test")
    assert client.access_key == "fake-other-access"
    assert client.account_id == "other-account"


def test_explicit_token_on_signing_endpoint_fails_before_network(polluted_environment, monkeypatch):
    client = SkillServiceClient(
        base_url="https://aicp.api.ksyun.com",
        token="fake-token",
        region="region-a",
        allow_env_fallback=False,
    )
    calls = []
    monkeypatch.setattr(client, "_requests", lambda: calls.append("network"))
    with pytest.raises(ValueError, match="requires signing credentials"):
        client.list_skills_by_space_id("space-a")
    assert calls == []


def test_explicit_signer_uses_only_selected_account(polluted_environment):
    client = SkillServiceClient(
        base_url="https://aicp.api.ksyun.com",
        region="region-a",
        access_key="fake-selected-access",
        secret_key="fake-selected-secret",
        allow_env_fallback=False,
    )
    headers = client._auth.sign_headers("GET", "https://aicp.api.ksyun.com/", {})
    assert "Credential=fake-selected-access/" in headers["Authorization"]
    assert "fake-other-access" not in headers["Authorization"]
    assert client.account_id == ""


def test_download_limit_stops_stream_and_closes_transport():
    reads = []
    closed = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(100):
                reads.append(index)
                yield b"x" * 65536

        def close(self):
            closed.append(True)

    client = SkillServiceClient(
        base_url="https://skills.example.test",
        region="region-a",
        allow_env_fallback=False,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body())),
    )
    client.get_skill_download_url = lambda _: "https://downloads.example.test/package.zip"
    skill = SkillRef(skill_id="skill-a", version_id="version-a", version="1", name="test")
    with pytest.raises(ValueError, match="download limit"):
        client.download_skill_archive(skill, max_bytes=100)
    assert reads == [0]
    assert closed == [True]


def test_download_http_failure_does_not_disclose_signed_url():
    client = SkillServiceClient(
        base_url="https://skills.example.test",
        region="region-a",
        allow_env_fallback=False,
        transport=httpx.MockTransport(lambda _: httpx.Response(403)),
    )
    client.get_skill_download_url = lambda _: (
        "https://downloads.example.test/package.zip?token=fake-signed-secret"
    )
    skill = SkillRef(skill_id="skill-a", version_id="version-a", version="1", name="test")
    with pytest.raises(ValueError) as raised:
        client.download_skill_archive(skill)
    assert "fake-signed-secret" not in str(raised.value)
