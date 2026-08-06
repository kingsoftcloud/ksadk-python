# -*- coding: utf-8 -*-
"""A2A 装进 create_runtime_app factory 的测试(契约 §8,review 修复)。

``RuntimeAppConfig.a2a.enabled`` 时,factory 应把 A2A 数据面端点(AgentCard /
JSONRPC / REST)装配进 app;不 enable 则不装配。runner 经惰性代理解析,装配期
不要求真实 runner。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ksadk.a2a.card import JSONRPC_PATH
from ksadk.a2a.routes import A2AConfig
from ksadk.server.app import _configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app


def _paths(app) -> set[str]:
    # 兼容 fastapi >= 0.139 的懒加载 ``_IncludedRouter``(见 test_app_factory.py)。
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


def test_factory_does_not_wire_a2a_when_disabled() -> None:
    app = create_runtime_app(RuntimeAppConfig(a2a=None), _configure_runtime_app)
    paths = _paths(app)
    assert JSONRPC_PATH not in paths
    assert app.state.runtime.a2a_server is None


def test_factory_wires_a2a_when_enabled() -> None:
    a2a_cfg = A2AConfig(
        enabled=True,
        agent_name="factory-agent",
        base_url="http://testserver",
        task_store_dsn="sqlite+aiosqlite:///:memory:",
    )
    app = create_runtime_app(RuntimeAppConfig(a2a=a2a_cfg), _configure_runtime_app)
    paths = _paths(app)
    # A2A 数据面端点已装配
    assert JSONRPC_PATH in paths
    assert any(p and p.startswith("/a2a/v1") for p in paths)
    assert app.state.runtime.a2a_server is not None

    # AgentCard 可访问(GET,无需真实 runner)
    client = TestClient(app)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    assert resp.json()["name"] == "factory-agent"
