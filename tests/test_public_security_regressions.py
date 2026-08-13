from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.model_proxy.config import ProxyConfig
from ksadk.model_proxy.server import create_app
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace


def test_workspace_open_path_requires_the_advertised_canonical_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = Workspace(root)

    assert workspace.matches_configured_root_path(str(root))
    assert not workspace.matches_configured_root_path(f"{root}/.")


def test_workspace_resolve_preserves_containment_for_absolute_and_symlink_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    inside = root / "agents"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    same_prefix_outside = tmp_path / "workspace-other"
    same_prefix_outside.mkdir()
    (root / "outside-link").symlink_to(outside, target_is_directory=True)
    workspace = Workspace(root)

    assert workspace.resolve(inside) == inside
    with pytest.raises(FileNotFoundError):
        workspace.resolve("missing", must_exist=True)
    with pytest.raises(StudioError, match="路径不在当前工作区内"):
        workspace.resolve("outside-link/private.txt")
    with pytest.raises(StudioError, match="路径不在当前工作区内"):
        workspace.resolve(same_prefix_outside / "private.txt")


def test_responses_unsupported_tools_error_does_not_echo_request_payload() -> None:
    app = create_app(ProxyConfig(api_key="test-key"))
    secret = "request-only-secret"

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "test-model",
                "input": "hello",
                "tools": [{"type": "web_search", "metadata": secret}],
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "type": "unsupported_tools",
            "message": "The request uses tools unsupported by the model upstream.",
        }
    }
    assert secret not in response.text
