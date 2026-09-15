from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app


def test_open_switches_runtime_to_second_workspace(tmp_path: Path) -> None:
    first = tmp_path / "project-a"
    second = tmp_path / "project-b"
    first.mkdir()
    second.mkdir()
    app = create_studio_app(first, security_enabled=False)
    with TestClient(app) as client:
        opened = client.post("/api/v1/workspaces:open", json={"path": str(second)})
        assert opened.status_code == 200
        payload = opened.json()
        assert payload["path"] == str(second.resolve())
        bootstrap = client.get("/api/v1/system/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["workspace"]["path"] == str(second.resolve())
