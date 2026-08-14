from __future__ import annotations

import pytest

from ksadk.common.kop_client import KOPClient, KOPError


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.request: dict | None = None

    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        return self.response


def test_kop_client_uses_configured_a2a_service_bearer_token(monkeypatch):
    monkeypatch.setenv("KSADK_A2A_SERVICE_TOKEN", "service-token")
    client = KOPClient(base_url="https://control.example.com")
    session = _Session(_Response(200, {"Code": 0, "Data": {}}))
    client._session = session

    client.post_action("ListAToASpaceAgents")

    assert session.request is not None
    assert session.request["headers"]["Authorization"] == "Bearer service-token"


def test_kop_client_rejects_non_success_http_status_even_when_code_is_zero():
    client = KOPClient(base_url="https://control.example.com")
    client._session = _Session(_Response(401, {"Code": 0, "Data": {}}))

    with pytest.raises(KOPError) as exc_info:
        client.post_action("ListAToASpaceAgents")

    assert exc_info.value.code == 401
