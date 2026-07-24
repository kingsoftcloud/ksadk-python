from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from ksadk.server.app import app

_ROUTE_MANIFEST_SHA256 = "51a60325487a422f10a5d82ad51d5d233e8d314956a7fd84cc91dcf7bc27019a"
_OPENAPI_OPERATIONS_SHA256 = "ac9086d1592d69d7eecd4260c2ca4bfa06b0daaed5cdf0653c4eb409204ed84d"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
_AGUI_PATHS = {"/agentengine/agui", "/agentengine/agui/health"}


def _flatten_routes(routes: Iterable[Any]) -> Iterable[Any]:
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from _flatten_routes(original_router.routes)
        else:
            yield route


def _route_manifest() -> list[list[str]]:
    manifest: list[list[str]] = []
    for route in _flatten_routes(app.routes):
        path = getattr(route, "path_format", getattr(route, "path", ""))
        name = getattr(route, "name", "")
        methods = sorted(getattr(route, "methods", ()) or ())
        if methods:
            manifest.extend([method, path, name] for method in methods)
        else:
            manifest.append(["WS", path, name])
    return manifest


def _openapi_operations() -> list[list[str]]:
    operations: list[list[str]] = []
    for path, path_item in app.openapi()["paths"].items():
        for method, operation in path_item.items():
            if method.lower() in _HTTP_METHODS:
                operations.append([method.upper(), path, operation.get("operationId", "")])
    return operations


def _without_agui(routes: Iterable[list[str]]) -> list[list[str]]:
    """Keep the legacy route baseline stable while asserting AG-UI explicitly.

    ``server.app`` mounts AG-UI lazily once a runner is installed, so the
    default-app facade may be observed before or after that installation
    depending on the test process. AG-UI is an intentional optional transport,
    not an accidental mutation of the legacy HTTP contract.
    """
    return [route for route in routes if route[1] not in _AGUI_PATHS]


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def test_runtime_route_manifest_preserves_legacy_contract() -> None:
    manifest = _route_manifest()
    legacy_manifest = _without_agui(manifest)
    agui_manifest = [route for route in manifest if route[1] in _AGUI_PATHS]

    assert len(legacy_manifest) == 72, json.dumps(manifest, indent=2, ensure_ascii=False)
    assert _sha256(legacy_manifest) == _ROUTE_MANIFEST_SHA256, json.dumps(
        legacy_manifest, indent=2, ensure_ascii=False
    )
    assert legacy_manifest[-9:] == [
        ["DELETE", "/api/{proxy_path}", "custom_api_proxy"],
        ["GET", "/api/{proxy_path}", "custom_api_proxy"],
        ["OPTIONS", "/api/{proxy_path}", "custom_api_proxy"],
        ["PATCH", "/api/{proxy_path}", "custom_api_proxy"],
        ["POST", "/api/{proxy_path}", "custom_api_proxy"],
        ["PUT", "/api/{proxy_path}", "custom_api_proxy"],
        ["GET", "/health", "health_check"],
        ["GET", "/list-apps", "list_apps"],
        ["GET", "/{requested_path}", "serve_agent_ui_static"],
    ]
    assert agui_manifest in (
        [],
        [
            ["POST", "/agentengine/agui", "langgraph_agent_endpoint"],
            ["GET", "/agentengine/agui/health", "health"],
        ],
    )


def test_runtime_openapi_operations_preserve_legacy_contract() -> None:
    operations = _openapi_operations()
    legacy_operations = _without_agui(operations)
    agui_operations = [operation for operation in operations if operation[1] in _AGUI_PATHS]
    legacy_paths = {
        path: path_item
        for path, path_item in app.openapi()["paths"].items()
        if path not in _AGUI_PATHS
    }

    assert len(legacy_paths) == 45
    assert len(legacy_operations) == 51
    assert _sha256(legacy_operations) == _OPENAPI_OPERATIONS_SHA256, json.dumps(
        legacy_operations, indent=2, ensure_ascii=False
    )
    assert agui_operations in (
        [],
        [
            ["POST", "/agentengine/agui", "langgraph_agent_endpoint"],
            ["GET", "/agentengine/agui/health", "health_agentengine_agui_health_get"],
        ],
    )
