from __future__ import annotations

import httpx
import pytest

from ksadk.skills.service_client import SkillServiceClient


def test_service_client_lists_skills_and_downloads_archive_with_mock_transport():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), request.headers.get("Authorization")))
        if request.url.path.endswith("/ListSkillsBySpaceId"):
            return httpx.Response(
                200,
                json={
                    "Code": 200,
                    "RequestId": "req-list",
                    "Data": {
                        "Skills": [
                            {"SkillId": "sk-1", "VersionId": "sv-1", "Version": "v1", "Name": "demo", "Status": "Active"}
                        ],
                    },
                },
            )
        if request.url.path.endswith("/GetSkillDownloadUrl"):
            return httpx.Response(200, json={"Code": 200, "Data": {"DownloadUrl": "https://download.example/skill.zip"}})
        if str(request.url) == "https://download.example/skill.zip":
            return httpx.Response(200, content=b"zip-bytes")
        return httpx.Response(404)

    client = SkillServiceClient(
        base_url="https://skill.example/api/v1",
        token="secret-token",
        transport=httpx.MockTransport(handler),
    )

    listing = client.list_skills_by_space_id("ss-1")
    archive = client.download_skill_archive(listing.skills[0])

    assert listing.request_id == "req-list"
    assert listing.skills[0].skill_id == "sk-1"
    assert archive == b"zip-bytes"
    assert requests[0] == (
        "GET",
        "https://skill.example/api/v1/ListSkillsBySpaceId?SpaceId=ss-1",
        "Bearer secret-token",
    )
    assert requests[1] == (
        "GET",
        "https://skill.example/api/v1/GetSkillDownloadUrl?SkillId=sk-1&VersionId=sv-1",
        "Bearer secret-token",
    )
    assert requests[-1] == ("GET", "https://download.example/skill.zip", None)


def test_service_client_downloads_no_version_skill_from_premade_endpoint():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.url.path.endswith("/ListSkillsBySpaceId"):
            return httpx.Response(
                200,
                json={
                    "Code": 200,
                    "RequestId": "req-list",
                    "Data": {
                        "Skills": [
                            {
                                "SkillId": "premade-pdf",
                                "VersionId": "",
                                "Version": "",
                                "Name": "pdf",
                                "Status": "AVAILABLE",
                            }
                        ],
                    },
                },
            )
        if request.url.path.endswith("/GetPremadeSkillDownloadUrl"):
            return httpx.Response(
                200,
                json={"Code": 200, "Data": {"DownloadUrl": "https://download.example/pdf.zip"}},
            )
        if str(request.url) == "https://download.example/pdf.zip":
            return httpx.Response(200, content=b"premade-zip-bytes")
        return httpx.Response(404)

    client = SkillServiceClient(
        base_url="https://skill.example/api/v1",
        transport=httpx.MockTransport(handler),
    )

    listing = client.list_skills_by_space_id("ss-public")
    archive = client.download_skill_archive(listing.skills[0])

    assert archive == b"premade-zip-bytes"
    assert requests[1] == (
        "GET",
        "https://skill.example/api/v1/GetPremadeSkillDownloadUrl?SkillId=premade-pdf&VersionId=",
    )


def test_service_client_lists_available_premade_skills_from_dedicated_endpoint():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.url.path.endswith("/ListAvailablePremadeSkills"):
            return httpx.Response(
                200,
                json={
                    "Code": 200,
                    "RequestId": "req-premade-list",
                    "Data": {
                        "Skills": [
                            {
                                "SkillId": "premade-pdf",
                                "VersionId": "",
                                "Version": "",
                                "Name": "pdf",
                                "Status": "AVAILABLE",
                                "ContentHash": "abc123",
                            },
                            {
                                "SkillId": "premade-xlsx",
                                "VersionId": "",
                                "Version": "",
                                "Name": "xlsx",
                                "Status": "AVAILABLE",
                                "ContentHash": "def456",
                            },
                        ],
                    },
                },
            )
        return httpx.Response(404)

    client = SkillServiceClient(
        base_url="https://skill.example/api/v1",
        transport=httpx.MockTransport(handler),
    )

    listing = client.list_available_premade_skills()

    assert listing.request_id == "req-premade-list"
    assert listing.space_id == "public"
    assert [skill.name for skill in listing.active_skills()] == ["pdf", "xlsx"]
    assert [skill.version_id for skill in listing.active_skills()] == ["", ""]
    assert requests == [
        ("GET", "https://skill.example/api/v1/ListAvailablePremadeSkills"),
    ]


def test_service_client_sends_account_header_for_direct_rest_service():
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"Code": 200, "Data": {"Skills": []}})

    client = SkillServiceClient(
        base_url="https://skill.example/api/v1",
        account_id="2000003485",
        transport=httpx.MockTransport(handler),
    )

    client.list_skills_by_space_id("ss-1")

    assert seen_headers["x-ksc-account-id"] == "2000003485"


def test_service_client_supports_custom_action_paths():
    client = SkillServiceClient(base_url="https://skill.example/root/")

    assert client.action_url("ListSkillsBySpaceId") == "https://skill.example/root/ListSkillsBySpaceId"


def test_service_client_normalizes_docs_and_openapi_urls():
    docs_client = SkillServiceClient(base_url="https://skill.example/agentengine/skill/docs#/SkillSpace")
    openapi_client = SkillServiceClient(base_url="https://skill.example/agentengine/skill/api/v1/openapi.json")

    assert (
        docs_client.action_url("ListSkillsBySpaceId")
        == "https://skill.example/agentengine/skill/api/v1/ListSkillsBySpaceId"
    )
    assert (
        openapi_client.action_url("ListSkillsBySpaceId")
        == "https://skill.example/agentengine/skill/api/v1/ListSkillsBySpaceId"
    )


def test_service_client_lists_skill_spaces_with_real_query_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://skill.example/api/v1/ListSkillSpaces?PageNumber=1&PageSize=20"
        return httpx.Response(
            200,
            json={
                "Code": 200,
                "Message": "ok",
                "RequestId": "req-space",
                "Data": {"Items": [{"Id": "ss-1", "Name": "demo"}], "TotalCount": 1},
            },
        )

    client = SkillServiceClient(
        base_url="https://skill.example/api/v1",
        transport=httpx.MockTransport(handler),
    )

    response = client.list_skill_spaces(page_number=1, page_size=20)

    assert response["Data"]["Items"][0]["Id"] == "ss-1"


def test_service_client_uses_registered_kop_action_for_aicp_skill_space_listing():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), dict(request.headers)))
        if request.url.params.get("Action") == "ListSkillsBySpaceId":
            return httpx.Response(
                200,
                json={
                    "Code": 200,
                    "RequestId": "req-kop-list",
                    "Data": {
                        "Skills": [
                            {
                                "SkillId": "sk-1",
                                "VersionId": "sv-1",
                                "Version": "v1",
                                "Name": "demo",
                                "Status": "AVAILABLE",
                            }
                        ],
                    },
                },
            )
        return httpx.Response(404)

    client = SkillServiceClient(
        base_url="http://aicp.inner.api.ksyun.com",
        account_id="2000003485",
        transport=httpx.MockTransport(handler),
    )

    listing = client.list_skills_by_space_id("ss-1")

    assert listing.space_id == "ss-1"
    assert listing.active_skills()[0].skill_id == "sk-1"
    method, url, headers = requests[0]
    assert method == "GET"
    assert url == (
        "http://aicp.inner.api.ksyun.com/"
        "?Action=ListSkillsBySpaceId&Version=2024-06-12"
        "&SpaceId=ss-1&PageNumber=1&PageSize=100"
    )
    assert headers["x-action"] == "ListSkillsBySpaceId"
    assert headers["x-version"] == "2024-06-12"
    assert headers["x-ksc-account-id"] == "2000003485"


def test_service_client_uses_kop_mode_for_internal_aicp_endpoint():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), dict(request.headers)))
        return httpx.Response(
            200,
            json={
                "Code": 200,
                "RequestId": "req-internal-kop",
                "Data": {"Skills": []},
            },
        )

    client = SkillServiceClient(
        base_url="http://aicp.internal.api.ksyun.com",
        account_id="2000003485",
        transport=httpx.MockTransport(handler),
    )

    listing = client.list_skills_by_space_id("ss-1")

    assert listing.space_id == "ss-1"
    method, url, headers = requests[0]
    assert method == "GET"
    assert url == (
        "http://aicp.internal.api.ksyun.com/"
        "?Action=ListSkillsBySpaceId&Version=2024-06-12"
        "&SpaceId=ss-1&PageNumber=1&PageSize=100"
    )
    assert headers["x-action"] == "ListSkillsBySpaceId"
    assert headers["x-version"] == "2024-06-12"
    assert headers["x-ksc-account-id"] == "2000003485"


def test_service_client_routes_pre_online_kop_requests_with_custom_source(monkeypatch):
    monkeypatch.setenv("KSADK_SKILL_SERVICE_REGION", "pre-online")
    monkeypatch.setenv("AGENTENGINE_PRE_CONTROL_REGION", "cn-beijing-6")
    monkeypatch.setenv("AGENTENGINE_PRE_CUSTOM_SOURCE", "pre")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), dict(request.headers)))
        return httpx.Response(
            200,
            json={
                "Code": 200,
                "RequestId": "req-pre-kop",
                "Data": {"Skills": []},
            },
        )

    client = SkillServiceClient(
        base_url="http://aicp.inner.api.ksyun.com",
        account_id="73398439",
        transport=httpx.MockTransport(handler),
    )

    listing = client.list_skills_by_space_id("ss-pre")

    assert listing.space_id == "ss-pre"
    method, url, headers = requests[0]
    assert method == "GET"
    assert url == (
        "http://aicp.inner.api.ksyun.com/"
        "?Action=ListSkillsBySpaceId&Version=2024-06-12"
        "&SpaceId=ss-pre&PageNumber=1&PageSize=100"
    )
    assert headers["x-ksc-region"] == "cn-beijing-6"
    assert headers["x-ksc-custom-source"] == "pre"
    assert headers["x-ksc-account-id"] == "73398439"


def test_service_client_uses_registered_kop_action_for_available_premade_skills():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), dict(request.headers)))
        if request.url.params.get("Action") == "ListAvailablePremadeSkills":
            return httpx.Response(
                200,
                json={
                    "Code": 200,
                    "RequestId": "req-kop-premade",
                    "Data": {
                        "Skills": [
                            {
                                "SkillId": "premade-pdf",
                                "VersionId": "",
                                "Version": "",
                                "Name": "pdf",
                                "Status": "AVAILABLE",
                            }
                        ],
                    },
                },
            )
        return httpx.Response(404)

    client = SkillServiceClient(
        base_url="http://aicp.inner.api.ksyun.com",
        account_id="2000003485",
        transport=httpx.MockTransport(handler),
    )

    listing = client.list_available_premade_skills()

    assert listing.space_id == "public"
    assert listing.active_skills()[0].skill_id == "premade-pdf"
    method, url, headers = requests[0]
    assert method == "GET"
    assert url == (
        "http://aicp.inner.api.ksyun.com/"
        "?Action=ListAvailablePremadeSkills&Version=2024-06-12"
    )
    assert headers["x-action"] == "ListAvailablePremadeSkills"
    assert headers["x-version"] == "2024-06-12"
    assert headers["x-ksc-account-id"] == "2000003485"


def test_service_client_kop_requires_credentials_without_mock_transport(monkeypatch):
    monkeypatch.delenv("KSADK_SKILL_SERVICE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("KSADK_SKILL_SERVICE_SECRET_KEY", raising=False)
    monkeypatch.delenv("KSYUN_ACCESS_KEY", raising=False)
    monkeypatch.delenv("KSYUN_SECRET_KEY", raising=False)

    client = SkillServiceClient(base_url="http://aicp.inner.api.ksyun.com")

    with pytest.raises(ValueError, match="requires signing credentials"):
        client.list_skill_spaces()
