"""Browser entry selection must never silently drop enabled workspace pages."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.errors import StudioError


@pytest.mark.parametrize("query", ["", "?workspacePage=teams&view=chat"])
def test_enabled_profile_enters_core_after_setting_the_local_session(tmp_path, monkeypatch, query):
    app = create_studio_app(tmp_path, session_token="test-entry-session")
    service = app.state.studio_service
    check = AsyncMock(return_value=True)
    monkeypatch.setattr(service.dsh_capabilities, "has_enabled_profile_plugins", check)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/" + query)
        assert response.status_code == 307
        assert response.headers["location"] == "/studio-core/" + query
        assert client.cookies.get("agentkit_studio_session") == "test-entry-session"
        assert "HttpOnly" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"
        check.assert_awaited_once()


@pytest.mark.parametrize("failure", [None, StudioError("DSH_TOOLCHAIN_MISSING", "toolchain absent", status_code=503)])
def test_plain_workspace_keeps_the_react_entry_without_requiring_core(tmp_path, monkeypatch, failure):
    app = create_studio_app(tmp_path)
    check = AsyncMock(return_value=False, side_effect=failure)
    monkeypatch.setattr(app.state.studio_service.dsh_capabilities, "has_enabled_profile_plugins", check)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert '/static/assets/' in response.text
        assert response.headers["cache-control"] == "no-store"


def test_fresh_core_link_bootstraps_then_enters_the_enabled_workspace(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path)
    monkeypatch.setattr(app.state.studio_service.dsh_capabilities, "has_enabled_profile_plugins", AsyncMock(return_value=True))
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/studio-core/?workspacePage=teams")
        assert response.status_code == 307
        assert response.headers["location"] == "/?workspacePage=teams"
        response = client.get(response.headers["location"])
        assert response.status_code == 307
        assert response.headers["location"] == "/studio-core/?workspacePage=teams"
        assert client.cookies.get("agentkit_studio_session")
