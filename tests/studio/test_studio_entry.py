"""Browser entry selection must never silently drop enabled workspace pages."""

import hashlib
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
        cookie_name = "agentkit_studio_session_" + hashlib.sha256(
            b"test-entry-session"
        ).hexdigest()[:16]
        assert client.cookies.get(cookie_name) == "test-entry-session"
        assert "HttpOnly" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"
        check.assert_awaited_once()


@pytest.mark.parametrize(
    "failure", [None, StudioError("DSH_TOOLCHAIN_MISSING", "toolchain absent", status_code=503)]
)
def test_plain_workspace_keeps_the_react_entry_without_requiring_core(
    tmp_path, monkeypatch, failure
):
    app = create_studio_app(tmp_path)
    check = AsyncMock(return_value=False, side_effect=failure)
    monkeypatch.setattr(
        app.state.studio_service.dsh_capabilities, "has_enabled_profile_plugins", check
    )
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "/static/assets/" in response.text
        assert response.headers["cache-control"] == "no-store"


def test_fresh_core_link_bootstraps_then_enters_the_enabled_workspace(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path)
    monkeypatch.setattr(
        app.state.studio_service.dsh_capabilities,
        "has_enabled_profile_plugins",
        AsyncMock(return_value=True),
    )
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/studio-core/?workspacePage=teams")
        assert response.status_code == 307
        assert response.headers["location"] == "/?workspacePage=teams"
        response = client.get(response.headers["location"])
        assert response.status_code == 307
        assert response.headers["location"] == "/studio-core/?workspacePage=teams"
        assert client.cookies.get("agentkit_studio_session")


def test_recovery_entry_is_independent_of_core_profile_and_assets(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, session_token="recovery-session")
    capabilities = app.state.studio_service.dsh_capabilities
    profile = AsyncMock(side_effect=AssertionError("Recovery must not inspect Profile"))
    lease = AsyncMock(side_effect=AssertionError("Recovery must not start Core"))
    monkeypatch.setattr(capabilities, "has_enabled_profile_plugins", profile)
    monkeypatch.setattr(capabilities, "connector_lease", lease)
    with TestClient(app, follow_redirects=False) as client:
        page = client.get("/studio-recovery/")
        assert page.status_code == 200
        assert "禁用 Teams" in page.text
        assert "/static/" not in page.text
        assert client.cookies.get("agentkit_studio_session") == "recovery-session"
        assert page.headers["cache-control"] == "no-store"
        status = client.get("/api/v1/plugin-ecosystems/dsh/recovery")
        assert status.status_code == 200
        assert status.json()["failure"] is None
        profile.assert_not_awaited()
        lease.assert_not_awaited()


def test_core_start_failure_opens_basic_workspace_and_keeps_recovery_available(
    tmp_path, monkeypatch
):
    from ksadk.plugins.teams.errors import TeamsError

    app = create_studio_app(tmp_path)
    capabilities = app.state.studio_service.dsh_capabilities
    monkeypatch.setattr(
        capabilities,
        "connector_lease",
        AsyncMock(
            side_effect=TeamsError("authority_in_use", "private-transport-detail", status=409)
        ),
    )
    with TestClient(app) as client:
        client.get("/studio-recovery/")
        failed = client.get("/studio-core/")
        assert failed.status_code == 200
        assert failed.url.path == "/studio-shell/"
        assert 'id="root"' in failed.text
        assert "private-transport-detail" not in failed.text
        assert client.get("/studio-recovery/").status_code == 200


def test_recovery_api_preserves_local_session_boundary(tmp_path):
    app = create_studio_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/plugin-ecosystems/dsh/recovery").status_code == 401
