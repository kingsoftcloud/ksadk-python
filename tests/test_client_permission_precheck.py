from __future__ import annotations

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

