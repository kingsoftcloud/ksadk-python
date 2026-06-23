import asyncio

import pytest

from ksadk.openclaw_gateway import DashboardAccessInfo, OpenClawGatewayClient
from ksadk.openclaw_gateway import OpenClawGatewayError


class _CapturingGatewayClient(OpenClawGatewayClient):
    def __init__(self):
        super().__init__(region="pre-online", agent_id="ar-demo-1")
        self.connect_request = None

    async def build_access_info(self, **_kwargs):
        return DashboardAccessInfo(
            agent_id="ar-demo-1",
            agent_name="demo",
            access_url="http://dashboard.example.com/s/link",
            ws_url="ws://dashboard.example.com/",
            cookie_header="sid=demo",
            origin="http://dashboard.example.com",
        )

    async def _connect_ws(self, _ws_url, _headers):
        return object()

    async def _wait_for_connect_challenge(self, *, timeout_ms=10_000):
        return "nonce-demo"

    async def request(self, method, params=None, *, timeout_ms=30_000):
        if method == "connect":
            self.connect_request = dict(params or {})
        return {"features": {"methods": []}}


def test_openclaw_gateway_client_uses_current_protocol_v4_for_managed_runtime():
    client = _CapturingGatewayClient()

    asyncio.run(client.connect())

    assert client.connect_request["minProtocol"] == 4
    assert client.connect_request["maxProtocol"] == 4


class _FakeCookieJar:
    def get_dict(self):
        return {"ae_ui_session": "sid-demo"}


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.cookies = _FakeCookieJar()
        self.calls = []

    def get(self, url, *, allow_redirects, timeout):
        self.calls.append(
            {
                "url": url,
                "allow_redirects": allow_redirects,
                "timeout": timeout,
            }
        )
        return self.response


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_build_access_info_accepts_short_link_redirect_without_following(monkeypatch):
    client = OpenClawGatewayClient(region="pre-online", agent_id="ar-demo-1")
    client.session = _FakeSession(_FakeResponse(302))

    async def fake_create_dashboard_access_link(**_kwargs):
        return {"access_url": "http://dashboard.example.com/s/link", "link_id": "link"}

    class FakeAgentEngineClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        create_dashboard_access_link = staticmethod(fake_create_dashboard_access_link)

    monkeypatch.setattr("ksadk.openclaw_gateway.AgentEngineClient", FakeAgentEngineClient)

    info = asyncio.run(client.build_access_info())

    assert info.cookie_header == "ae_ui_session=sid-demo"
    assert client.session.calls == [
        {
            "url": "http://dashboard.example.com/s/link",
            "allow_redirects": False,
            "timeout": 30,
        }
    ]


def test_build_access_info_rejects_non_redirect_short_link(monkeypatch):
    client = OpenClawGatewayClient(region="pre-online", agent_id="ar-demo-1")
    client.session = _FakeSession(_FakeResponse(200))

    async def fake_create_dashboard_access_link(**_kwargs):
        return {"access_url": "http://dashboard.example.com/s/link", "link_id": "link"}

    class FakeAgentEngineClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        create_dashboard_access_link = staticmethod(fake_create_dashboard_access_link)

    monkeypatch.setattr("ksadk.openclaw_gateway.AgentEngineClient", FakeAgentEngineClient)

    with pytest.raises(OpenClawGatewayError, match="HTTP 200"):
        asyncio.run(client.build_access_info())
