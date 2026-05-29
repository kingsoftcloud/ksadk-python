from __future__ import annotations

import logging

import pytest

from ksadk.api.client import AgentEngineAPIError, AgentEngineClient


@pytest.fixture(autouse=True)
def clear_permission_probe_cache():
    cache = getattr(AgentEngineClient, "_permission_probe_cache", None)
    if isinstance(cache, dict):
        cache.clear()
    yield
    cache = getattr(AgentEngineClient, "_permission_probe_cache", None)
    if isinstance(cache, dict):
        cache.clear()


def _build_client() -> AgentEngineClient:
    return AgentEngineClient(
        base_url="https://aicp.api.ksyun.com",
        access_key="ak",
        secret_key="sk",
        region="cn-beijing-6",
    )


@pytest.mark.asyncio
async def test_list_agents_prechecks_default_role(monkeypatch):
    client = _build_client()
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, body: dict | None = None):
        calls.append((method, path, dict(body or {})))
        if path.endswith("/CheckIamRole"):
            return {
                "Code": 0,
                "Message": "Success",
                "Data": {"HasPermission": True, "RoleName": "KsyunAgentEngineDefaultRole"},
            }
        if path.endswith("/ListAgents"):
            return {
                "Code": 0,
                "Message": "Success",
                "Data": {"Agents": [], "Total": 0, "Page": 1, "PageSize": 20},
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.list_agents()

    assert result["agents"] == []
    assert calls[0][1].endswith("/CheckIamRole")
    assert calls[0][2] == {"RoleName": "KsyunAgentEngineDefaultRole"}
    assert calls[1][1].endswith("/ListAgents")


@pytest.mark.asyncio
async def test_permission_denied_stops_main_request(monkeypatch):
    client = _build_client()
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, body: dict | None = None):
        calls.append((method, path, dict(body or {})))
        if path.endswith("/CheckIamRole"):
            return {
                "Code": 403,
                "Message": "当前账号没有 KsyunAgentEngineDefaultRole 权限",
                "Data": {"HasPermission": False, "RoleName": "KsyunAgentEngineDefaultRole"},
            }
        raise AssertionError("main request should not be sent")

    monkeypatch.setattr(client, "_request", fake_request)

    with pytest.raises(AgentEngineAPIError, match="当前账号没有 KsyunAgentEngineDefaultRole 权限"):
        await client.list_agents()

    assert calls == [
        (
            "POST",
            "/agentengine/api/v1/CheckIamRole",
            {"RoleName": "KsyunAgentEngineDefaultRole"},
        )
    ]


@pytest.mark.asyncio
async def test_probe_failure_is_fail_open(monkeypatch):
    client = _build_client()
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, body: dict | None = None):
        calls.append((method, path, dict(body or {})))
        if path.endswith("/CheckIamRole"):
            raise RuntimeError("HTTP 503 POST https://aicp.api.ksyun.com: probe unavailable")
        if path.endswith("/GetAgent"):
            return {
                "Code": 0,
                "Message": "Success",
                "Data": {"Basic": {"AgentId": "ar-demo"}},
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.get_agent(agent_id="ar-demo")

    assert result["basic"]["agent_id"] == "ar-demo"
    assert [path for _, path, _ in calls] == [
        "/agentengine/api/v1/CheckIamRole",
        "/agentengine/api/v1/GetAgent",
    ]


@pytest.mark.asyncio
async def test_permission_probe_uses_cache(monkeypatch):
    client = _build_client()
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, body: dict | None = None):
        calls.append((method, path, dict(body or {})))
        if path.endswith("/CheckIamRole"):
            return {
                "Code": 0,
                "Message": "Success",
                "Data": {"HasPermission": True, "RoleName": "KsyunAgentEngineDefaultRole"},
            }
        if path.endswith("/ListAgents"):
            return {
                "Code": 0,
                "Message": "Success",
                "Data": {"Agents": [], "Total": 0, "Page": 1, "PageSize": 20},
            }
        if path.endswith("/GetAgent"):
            return {
                "Code": 0,
                "Message": "Success",
                "Data": {"Basic": {"AgentId": "ar-demo"}},
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    await client.list_agents()
    await client.get_agent(agent_id="ar-demo")

    assert [path for _, path, _ in calls].count("/agentengine/api/v1/CheckIamRole") == 1


@pytest.mark.asyncio
async def test_create_agent_precheck_uses_explicit_iam_role(monkeypatch):
    client = _build_client()
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, body: dict | None = None):
        calls.append((method, path, dict(body or {})))
        if path.endswith("/CheckIamRole"):
            return {
                "Code": 0,
                "Message": "Success",
                "Data": {"HasPermission": True, "RoleName": "CustomRuntimeRole"},
            }
        if path.endswith("/CreateAgentProduct"):
            return {
                "Code": 0,
                "Message": "Success",
                "Data": {"AgentId": "ar-new"},
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    await client.create_agent(
        {
            "name": "demo-agent",
            "framework": "langgraph",
            "artifact_type": "Code",
            "artifact_path": "ks3://demo-bucket/code.zip",
            "region": "cn-beijing-6",
            "auth_type": "Iam",
            "iam_role": "CustomRuntimeRole",
        }
    )

    assert calls[0] == (
        "POST",
        "/agentengine/api/v1/CheckIamRole",
        {"RoleName": "CustomRuntimeRole"},
    )


def test_request_parses_kop_auth_error_payload(monkeypatch, caplog):
    client = AgentEngineClient(
        base_url="https://aicp.api.ksyun.com",
        access_key="ak",
        secret_key="sk",
        region="cn-beijing-6",
    )

    class _FakeResponse:
        status_code = 400
        text = (
            '{"RequestId":"req-missing-ak","Error":{"Code":"MissingAccesskey",'
            '"Message":"Access Key is Missing","Type":"Sender"}}'
        )

        def json(self):
            return {
                "RequestId": "req-missing-ak",
                "Error": {
                    "Code": "MissingAccesskey",
                    "Message": "Access Key is Missing",
                    "Type": "Sender",
                },
            }

    class _FakeSession:
        def request(self, **_kwargs):
            return _FakeResponse()

    monkeypatch.setattr(client, "_get_session", lambda: _FakeSession())

    with caplog.at_level(logging.WARNING, logger="ksadk.api.client"):
        with pytest.raises(AgentEngineAPIError) as exc:
            client._request("POST", "/agentengine/api/v1/GetAgent", {"AgentId": "ar-demo"})

    assert exc.value.code == 400
    assert exc.value.details["remote_error_code"] == "MissingAccesskey"
    assert exc.value.details["request_id"] == "req-missing-ak"
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_request_honors_curl_ssl_insecure_for_control_plane(monkeypatch):
    client = AgentEngineClient(
        base_url="https://aicp.api.ksyun.com",
        access_key="ak",
        secret_key="sk",
        region="cn-beijing-6",
    )
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = '{"Code":0,"Data":{"Ok":true}}'

        def json(self):
            return {"Code": 0, "Data": {"Ok": True}}

    class _FakeSession:
        def request(self, **kwargs):
            captured.update(kwargs)
            return _FakeResponse()

    monkeypatch.setenv("CURL_SSL_INSECURE", "1")
    monkeypatch.setattr(client, "_get_session", lambda: _FakeSession())

    result = client._request("POST", "/agentengine/api/v1/GetAgent", {"AgentId": "ar-demo"})

    assert result["Data"]["Ok"] is True
    assert captured["verify"] is False


def test_request_retries_inner_endpoint_for_inner_account(monkeypatch):
    client = AgentEngineClient(
        base_url="https://aicp.api.ksyun.com",
        access_key="ak",
        secret_key="sk",
        region="cn-beijing-6",
    )
    urls: list[str] = []

    class _FakeResponse:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text

        def json(self):
            return {"Code": 0, "Data": {"AgentId": "ar-inner"}}

    class _FakeSession:
        def request(self, **kwargs):
            urls.append(kwargs["url"])
            if len(urls) == 1:
                return _FakeResponse(
                    403,
                    (
                        '{"RequestId":"req-inner","Error":{'
                        '"Code":"InnerAccountCanOnlyAccessThroughIntranet",'
                        '"Message":"The inner account can only access through intranet",'
                        '"Type":"Sender"}}'
                    ),
                )
            return _FakeResponse(200, '{"Code":0,"Data":{"AgentId":"ar-inner"}}')

    monkeypatch.setattr(client, "_get_session", lambda: _FakeSession())

    result = client._request("POST", "/agentengine/api/v1/CreateAgentProduct", {"Name": "demo"})

    assert result["Data"]["AgentId"] == "ar-inner"
    assert urls == [
        "https://aicp.api.ksyun.com/?Action=CreateAgentProduct&Version=2024-06-12",
        "http://aicp.inner.api.ksyun.com/?Action=CreateAgentProduct&Version=2024-06-12",
    ]
    assert client.base_url == "http://aicp.inner.api.ksyun.com"


def test_auto_detected_public_endpoint_retries_inner_for_inner_account(monkeypatch):
    monkeypatch.delenv("AGENTENGINE_SERVER_URL", raising=False)
    monkeypatch.setattr(AgentEngineClient, "_is_connectable", staticmethod(lambda *_args, **_kwargs: False))
    client = AgentEngineClient(
        access_key="ak",
        secret_key="sk",
        region="cn-beijing-6",
    )
    urls: list[str] = []

    class _FakeResponse:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text

        def json(self):
            return {"Code": 0, "Data": {"AgentId": "ar-inner"}}

    class _FakeSession:
        def request(self, **kwargs):
            urls.append(kwargs["url"])
            if len(urls) == 1:
                return _FakeResponse(
                    403,
                    (
                        '{"RequestId":"req-inner","Error":{'
                        '"Code":"InnerAccountCanOnlyAccessThroughIntranet",'
                        '"Message":"The inner account can only access through intranet",'
                        '"Type":"Sender"}}'
                    ),
                )
            return _FakeResponse(200, '{"Code":0,"Data":{"AgentId":"ar-inner"}}')

    assert client.base_url == "https://aicp.api.ksyun.com"
    monkeypatch.setattr(client, "_get_session", lambda: _FakeSession())

    result = client._request("POST", "/agentengine/api/v1/CreateAgentProduct", {"Name": "demo"})

    assert result["Data"]["AgentId"] == "ar-inner"
    assert urls == [
        "https://aicp.api.ksyun.com/?Action=CreateAgentProduct&Version=2024-06-12",
        "http://aicp.inner.api.ksyun.com/?Action=CreateAgentProduct&Version=2024-06-12",
    ]
    assert client.base_url == "http://aicp.inner.api.ksyun.com"


def test_action_raw_request_retries_inner_endpoint_for_inner_account(monkeypatch):
    client = AgentEngineClient(
        base_url="https://aicp.api.ksyun.com",
        access_key="ak",
        secret_key="sk",
        region="cn-beijing-6",
    )
    urls: list[str] = []

    class _FakeResponse:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text

    class _FakeSession:
        def request(self, **kwargs):
            urls.append(kwargs["url"])
            if len(urls) == 1:
                return _FakeResponse(
                    403,
                    (
                        '{"RequestId":"req-inner","Error":{'
                        '"Code":"InnerAccountCanOnlyAccessThroughIntranet",'
                        '"Message":"The inner account can only access through intranet",'
                        '"Type":"Sender"}}'
                    ),
                )
            return _FakeResponse(200, '{"Code":0}')

    monkeypatch.setattr(client, "_get_session", lambda: _FakeSession())

    response = client._action_raw_request("GET", "ExportWorkspaceZip")

    assert response.status_code == 200
    assert urls == [
        "https://aicp.api.ksyun.com/?Action=ExportWorkspaceZip&Version=2024-06-12",
        "http://aicp.inner.api.ksyun.com/?Action=ExportWorkspaceZip&Version=2024-06-12",
    ]
    assert client.base_url == "http://aicp.inner.api.ksyun.com"


def test_permission_probe_auth_failure_is_quiet(monkeypatch, caplog):
    client = _build_client()
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")

    def fake_request(_method: str, _path: str, _body: dict | None = None):
        raise AgentEngineAPIError(
            400,
            "Access Key is Missing",
            details={
                "http_status": 400,
                "remote_error_code": "MissingAccesskey",
                "remote_error_message": "Access Key is Missing",
                "request_id": "req-missing-ak",
            },
        )

    monkeypatch.setattr(client, "_request", fake_request)

    with caplog.at_level(logging.WARNING, logger="ksadk.api.client"):
        client._maybe_precheck_permission("GetAgent", {"AgentId": "ar-demo"})

    assert not [record for record in caplog.records if "Permission probe failed" in record.message]
