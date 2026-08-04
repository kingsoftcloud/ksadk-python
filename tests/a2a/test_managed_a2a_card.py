# -*- coding: utf-8 -*-
"""Discovery-only managed A2A card mount tests (a2a-runtime-inbound-wiring v1).

``ManagedA2ACardMount`` mounts ``GET /.well-known/agent-card.json`` and nothing
else — no JSON-RPC, no identity middleware, no TaskStore. It must:

* mount whenever ``KSADK_A2A_RUNTIME_ID`` is set, even without ``KSADK_A2A_AGENT_ID``
  (breaks the hosted registration chicken-and-egg);
* NOT mount any JSON-RPC / REST task routes (v1 scope);
* return a wire-1.0-conformant card with the injected name/version/base_url;
* fall back through the name fallback chain when ``KSADK_A2A_AGENT_NAME`` is absent;
* fill a ``general`` skill when none are injected (delegated to build_agent_card).
"""

from __future__ import annotations

import importlib
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from ksadk.a2a.card import JSONRPC_PATH
from ksadk.managed_a2a_card import (
    ManagedA2ACardMount,
    build_managed_a2a_card_if_configured,
)
from ksadk.server.app import _configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app


def _paths(app) -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            for sub in getattr(original, "routes", []):
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    paths.add(sub_path)
    return paths


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in (
        "KSADK_A2A_AGENT_ID",
        "KSADK_A2A_ACCOUNT_ID",
        "KSADK_A2A_TENANT_ID",
        "KSADK_A2A_RUNTIME_ID",
        "KSADK_A2A_INTERNAL_BASE_URL",
        "KSADK_A2A_AGENT_NAME",
        "KSADK_A2A_AGENT_VERSION",
        "AGENTENGINE_MANAGED_RUNTIME_NAME",
        "AGENTENGINE_MANAGED_RUNTIME_VERSION",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def test_returns_none_when_agent_id_absent(_clean_env: None) -> None:
    assert build_managed_a2a_card_if_configured() is None


def test_mounts_card_without_a2a_agent_id(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 discovery card mounts before the A2A Agent is registered."""
    monkeypatch.setenv("KSADK_A2A_RUNTIME_ID", "ar-test-runtime")
    monkeypatch.setenv("KSADK_A2A_INTERNAL_BASE_URL", "http://runtime.internal:8080")

    mount = build_managed_a2a_card_if_configured()
    assert isinstance(mount, ManagedA2ACardMount)
    assert mount.config.base_url == "http://runtime.internal:8080"

    app = create_runtime_app(RuntimeAppConfig(a2a=mount), _configure_runtime_app)
    paths = _paths(app)
    assert "/.well-known/agent-card.json" in paths
    # v1 scope: no JSON-RPC / REST task routes
    assert JSONRPC_PATH not in paths
    assert not any(p.startswith("/a2a/v1") for p in paths)


def test_card_payload_uses_injected_name_version(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_A2A_RUNTIME_ID", "ar-test-runtime")
    monkeypatch.setenv("KSADK_A2A_AGENT_NAME", "weather-agent")
    monkeypatch.setenv("KSADK_A2A_AGENT_VERSION", "2.3.0")
    monkeypatch.setenv("KSADK_A2A_INTERNAL_BASE_URL", "http://runtime.internal:8080")

    mount = build_managed_a2a_card_if_configured()
    assert mount is not None
    app = create_runtime_app(RuntimeAppConfig(a2a=mount), _configure_runtime_app)

    resp = TestClient(app).get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "weather-agent"
    assert body["version"] == "2.3.0"
    # skills 非空(build_agent_card 空 skills 自动补 general)
    assert body["skills"], "card must have at least one skill"


def test_name_fallback_chain(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_A2A_RUNTIME_ID", "ar-fallback-runtime")
    monkeypatch.setenv("KSADK_A2A_INTERNAL_BASE_URL", "http://runtime.internal:8080")

    # 无 KSADK_A2A_AGENT_NAME,无 AGENTENGINE_MANAGED_RUNTIME_NAME → fallback 到 runtime_id
    mount = build_managed_a2a_card_if_configured()
    assert mount is not None
    assert mount.config.agent_name == "ar-fallback-runtime"

    # 加 AGENTENGINE_MANAGED_RUNTIME_NAME → 优先用它
    monkeypatch.setenv("AGENTENGINE_MANAGED_RUNTIME_NAME", "managed-runtime-12")
    mount = build_managed_a2a_card_if_configured()
    assert mount is not None
    assert mount.config.agent_name == "managed-runtime-12"

    # KSADK_A2A_AGENT_NAME 最优先
    monkeypatch.setenv("KSADK_A2A_AGENT_NAME", "explicit-name")
    mount = build_managed_a2a_card_if_configured()
    assert mount is not None
    assert mount.config.agent_name == "explicit-name"


def test_internal_base_url_default(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_A2A_RUNTIME_ID", "ar-test-runtime")
    mount = build_managed_a2a_card_if_configured()
    assert mount is not None
    assert mount.config.base_url == "http://localhost:8080"


def test_start_stop_are_noop(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("KSADK_A2A_RUNTIME_ID", "ar-test-runtime")
    mount = build_managed_a2a_card_if_configured()
    assert mount is not None
    assert asyncio.run(mount.start()) is None
    assert asyncio.run(mount.stop()) is None
