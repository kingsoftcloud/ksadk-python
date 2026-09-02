"""Studio exposes DSH and Codex plugin lifecycles only."""

import hashlib
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.plugins.dsh_toolchain import DshToolchainUnavailableError
from ksadk.studio import api_plugin_routes
from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService

_SESSION = "session-token-that-is-long-enough"
_CSRF = "csrf-token-that-is-long-enough"


def _login(client: TestClient) -> None:
    response = client.post("/api/v1/system/session", json={"token": _SESSION})
    assert response.status_code == 200


def test_plugin_api_has_no_native_package_management_surface(tmp_path: Path) -> None:
    with TestClient(
        create_studio_app(tmp_path, session_token=_SESSION, csrf_token=_CSRF)
    ) as client:
        _login(client)
        assert client.get("/api/v1/plugins").status_code == 404
        headers = {"X-CSRF-Token": _CSRF}
        assert (
            client.post(
                "/api/v1/plugins:validate", json={"source": "/tmp/x"}, headers=headers
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/plugins:install", json={"source": "/tmp/x"}, headers=headers
            ).status_code
            == 404
        )


def test_plugin_api_keeps_dsh_default_and_codex_compatibility_inventory(tmp_path: Path) -> None:
    with TestClient(create_studio_app(tmp_path, session_token=_SESSION)) as client:
        _login(client)
        dsh = client.get("/api/v1/plugin-ecosystems/dsh/plugins")
        codex = client.get("/api/v1/plugin-ecosystems/codex/plugins")
    assert dsh.status_code == 200
    assert dsh.json()["ecosystem"] == "dsh"
    assert codex.status_code == 200
    assert codex.json()["ecosystem"] == "codex"


def test_dsh_options_keep_explicit_binary_above_managed_toolchain(
    tmp_path: Path, monkeypatch
) -> None:
    explicit = tmp_path / "explicit-dsh"
    monkeypatch.setenv("KSADK_DSH_BIN", str(explicit))

    class _MustNotResolveManaged:
        def __init__(self) -> None:
            raise AssertionError("explicit DSH binary must bypass managed discovery")

    monkeypatch.setattr(
        api_plugin_routes, "DshToolchainManager", _MustNotResolveManaged
    )

    _home, _profile, command, _mode = api_plugin_routes._studio_dsh_options(
        StudioService(tmp_path)
    )

    assert command == (str(explicit),)


def test_plugin_page_uses_ready_managed_dsh_without_manual_binary_env(
    tmp_path: Path, monkeypatch
) -> None:
    managed = tmp_path / "managed-dsh"
    managed.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--version*) echo 0.1.1-rc.2;;\n"
        "  *) exit 2;;\n"
        "esac\n",
        encoding="utf-8",
    )
    managed.chmod(0o700)
    monkeypatch.delenv("KSADK_DSH_BIN", raising=False)

    class _ManagedToolchain:
        def require_command(self) -> tuple[str, ...]:
            return (str(managed),)

    monkeypatch.setattr(api_plugin_routes, "DshToolchainManager", _ManagedToolchain)

    with TestClient(create_studio_app(tmp_path, session_token=_SESSION)) as client:
        _login(client)
        response = client.get("/api/v1/plugin-ecosystems/dsh/plugins")

    assert response.status_code == 200
    assert response.json()["host"] == {
        "hostId": "deepseek-harness",
        "available": True,
        "status": "available",
        "version": "0.1.1-rc.2",
        "protocol": "dsh.profile/v1",
        "homeMode": "workspace-isolated",
    }
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["pluginId"] == "@kingsoftcloud/ksadk-codex-provider"
    assert items[0]["displayName"] == "@kingsoftcloud/ksadk-codex-provider"
    assert items[0]["enabled"] is True


def test_dsh_options_fall_back_to_bridge_path_lookup_when_managed_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("KSADK_DSH_BIN", raising=False)
    fallback = tmp_path / "dsh"
    fallback.write_text("#!/bin/sh\necho 0.1.0\n", encoding="utf-8")
    fallback.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    class _UnavailableToolchain:
        def require_command(self) -> tuple[str, ...]:
            raise DshToolchainUnavailableError("not installed")

    monkeypatch.setattr(api_plugin_routes, "DshToolchainManager", _UnavailableToolchain)

    _home, _profile, command, _mode = api_plugin_routes._studio_dsh_options(
        StudioService(tmp_path)
    )

    assert command is None
    with TestClient(create_studio_app(tmp_path, session_token=_SESSION)) as client:
        _login(client)
        response = client.get("/api/v1/plugin-ecosystems/dsh/plugins")
    assert response.json()["host"]["available"] is True
    assert response.json()["host"]["version"] == "0.1.0"


def test_dsh_profile_projects_and_serves_only_enabled_digest_fenced_client_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    dsh_home = tmp_path / "dsh-home"
    profile = dsh_home / "profiles" / "studio"
    plugin_name = "@example/studio-client"
    package = profile / "node_modules" / "@example" / "studio-client"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {plugin_name: "1.0.0"},
                "dsh": {"profile": {"bundles": [plugin_name]}},
            }
        ),
        encoding="utf-8",
    )
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": plugin_name,
                "version": "1.0.0",
                "exports": {"./client": {"default": "./lib/client.js"}},
                "dsh": {
                    "bundle": {"patch": "./cordis.patch.yml"},
                    "client": {"platform": "web", "external": ["react"]},
                },
            }
        ),
        encoding="utf-8",
    )
    (package / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    client = (
        b'window.__ModuleLoader__.load({id:"@example/studio-client",'
        b"factory:()=>({apply(){}})})"
    )
    (package / "lib").mkdir()
    (package / "lib" / "client.js").write_bytes(client)
    dsh = tmp_path / "dsh-fixture"
    dsh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--version*) echo 0.1.2-alpha.1;;\n"
        "  *--dump-config*) echo 'graph: fixture';;\n"
        "esac\n",
        encoding="utf-8",
    )
    dsh.chmod(0o700)
    monkeypatch.setenv("KSADK_DSH_HOME", str(dsh_home))
    monkeypatch.setenv("KSADK_DSH_BIN", str(dsh))

    with TestClient(
        create_studio_app(tmp_path, session_token=_SESSION, csrf_token=_CSRF)
    ) as client_api:
        _login(client_api)
        projection = client_api.get("/api/v1/plugin-ecosystems/dsh/profile")
        assert projection.status_code == 200
        bundle = projection.json()["clientBundles"][0]
        assert bundle["pluginId"] == plugin_name
        assert bundle["compatible"] is True
        assert bundle["digest"] == f"sha256:{hashlib.sha256(client).hexdigest()}"

        artifact = client_api.get(bundle["url"])
        assert artifact.status_code == 200
        assert artifact.content == client
        assert artifact.headers["x-content-type-options"] == "nosniff"

        disabled = client_api.post(
            "/api/v1/plugin-ecosystems/dsh/plugins/@example/studio-client:disable",
            headers={"X-CSRF-Token": _CSRF},
        )
        assert disabled.status_code == 200
        assert client_api.get("/api/v1/plugin-ecosystems/dsh/profile").json()["clientBundles"] == []
        assert client_api.get(bundle["url"]).status_code == 404
