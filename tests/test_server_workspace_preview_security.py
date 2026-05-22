from __future__ import annotations

import io
import importlib
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

appmod = importlib.import_module("ksadk.server.app")


def _client_with_workspace(monkeypatch, tmp_path: Path) -> tuple[TestClient, Path]:
    session_dir = tmp_path / "session"
    workspace = session_dir / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(appmod, "resolve_local_session_dir", lambda: session_dir)
    return TestClient(appmod.app), workspace


def test_workspace_html_route_applies_sandbox_csp(monkeypatch, tmp_path: Path):
    client, workspace = _client_with_workspace(monkeypatch, tmp_path)
    (workspace / "index.html").write_text("<html><head></head><body>ok</body></html>", encoding="utf-8")

    response = client.get("/agentengine/api/v1/ws/agent-1/index.html")

    assert response.status_code == 200
    csp = response.headers.get("content-security-policy", "")
    assert "sandbox allow-scripts allow-downloads" in csp
    assert "connect-src 'none'" in csp
    assert "script-src 'unsafe-inline' 'unsafe-eval' 'self' https:" in csp
    assert "img-src data: blob: 'self' https:" in csp
    assert '<base href="/_ksadk/workspace/v1/files/">' in response.text
    assert "data-ksadk-preview-anchor-handler" in response.text


def test_export_workspace_zip_does_not_follow_symlink_escape(monkeypatch, tmp_path: Path):
    client, workspace = _client_with_workspace(monkeypatch, tmp_path)
    (workspace / "safe.txt").write_text("safe", encoding="utf-8")
    outside_secret = tmp_path / "secret.txt"
    outside_secret.write_text("secret", encoding="utf-8")
    (workspace / "leak.txt").symlink_to(outside_secret)

    response = client.get("/agentengine/api/v1/ExportWorkspaceZip")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "safe.txt" in names
        assert "leak.txt" not in names
