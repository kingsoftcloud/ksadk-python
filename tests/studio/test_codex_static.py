from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app


def _local_assets(markup: str) -> list[str]:
    return re.findall(r'(?:src|href)="(/static/[^"]+)"', markup)


def test_studio_serves_the_react_shell_and_resolvable_production_assets(
    tmp_path: Path,
) -> None:
    app = create_studio_app(tmp_path, security_enabled=False)

    with TestClient(app) as client:
        page = client.get("/")
        assets = {path: client.get(path) for path in _local_assets(page.text)}

    assert page.status_code == 200
    assert '<div id="root"></div>' in page.text
    assert re.search(
        r'<script type="module"[^>]+src="/static/assets/[^"]+\.js"',
        page.text,
    )
    assert re.search(
        r'<link rel="stylesheet"[^>]+href="/static/assets/[^"]+\.css"',
        page.text,
    )
    assert assets
    assert {path: response.status_code for path, response in assets.items()} == {
        path: 200 for path in assets
    }


def test_production_assets_do_not_include_the_removed_shared_chat_frontend(
    tmp_path: Path,
) -> None:
    app = create_studio_app(tmp_path, security_enabled=False)

    with TestClient(app) as client:
        page = client.get("/")
        theme = client.get("/static/shared-chat.css")

    assert theme.status_code == 404
    assert "ksadk-web" not in page.text
