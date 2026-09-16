from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from ksadk.plugins.teams.errors import TeamsError
from ksadk.studio.teams_remote import RemoteStudioTeamsInstallation


def installation(environment):
    studio = SimpleNamespace(
        workspace_plugins=Mock(),
        configuration=SimpleNamespace(environment=lambda: environment),
        cloud=SimpleNamespace(gateway=SimpleNamespace(client=Mock())),
    )
    return RemoteStudioTeamsInstallation(studio, "https://teams.example.test/teams")


@pytest.mark.asyncio
async def test_explicit_bearer_does_not_require_unrelated_ak_sk_configuration():
    value = installation({"KSADK_TEAMS_ACCESS_TOKEN": "test-only-token"})
    try:
        headers = await value._client().request_headers("GET", "/lifecycle")
        assert headers["Authorization"] == "Bearer test-only-token"
        value.studio.cloud.gateway.client._auth.sign_headers.assert_not_called()
    finally:
        await value.client.close()


@pytest.mark.asyncio
async def test_remote_proxy_rejects_encoded_path_escape_before_signing():
    value = installation({})
    value.client = SimpleNamespace(request_headers=AsyncMock())
    app = FastAPI()
    app.include_router(value._router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/groups/%252e%252e/nodes")
        assert response.status_code == 400
        value.client.request_headers.assert_not_awaited()


@pytest.mark.asyncio
async def test_signing_failure_is_a_recoverable_response():
    value = installation({})
    value.client = SimpleNamespace(
        request_headers=AsyncMock(
            side_effect=TeamsError("teams_credentials_required", "missing", status=401)
        )
    )
    app = FastAPI()
    app.include_router(value._router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/groups")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "teams_credentials_required"
