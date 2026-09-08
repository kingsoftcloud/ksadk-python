from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from ksadk.server import RuntimeAppConfig, configure_runtime_app, create_runtime_app

app = create_runtime_app(RuntimeAppConfig(), configure_runtime_app)

_ROUTE_MANIFEST_SHA256 = "96dee853641258bdd4aacf17ba634f499f6341975f1662ad170678835bec6a74"
_OPENAPI_OPERATIONS_SHA256 = "bcd977f5e2550871e6b603007aa7df085e930048be1c680fafea52c3012319a0"
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

    AG-UI is an intentional optional transport and is excluded from the stable
    default HTTP contract unless explicitly configured on RuntimeAppConfig.
    """
    return [route for route in routes if route[1] not in _AGUI_PATHS]


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def test_runtime_route_manifest_preserves_legacy_contract() -> None:
    manifest = _route_manifest()
    legacy_manifest = _without_agui(manifest)
    agui_manifest = [route for route in manifest if route[1] in _AGUI_PATHS]

    assert len(legacy_manifest) == 76, json.dumps(manifest, indent=2, ensure_ascii=False)
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
    assert [route for route in legacy_manifest if route[1].startswith("/agent-kernel/v1/")] == [
        ["POST", "/agent-kernel/v1/SubmitAgentControl", "submit_agent_control"],
        ["POST", "/agent-kernel/v1/GetAgentStatus", "get_agent_status"],
        ["GET", "/agent-kernel/v1/SubscribeSessionEvents", "subscribe_session_events"],
        ["GET", "/agent-kernel/v1/health", "kernel_health"],
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

    assert len(legacy_paths) == 49
    assert len(legacy_operations) == 55
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
