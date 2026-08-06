"""create_runtime_app factory 一致性与隔离性测试 (goal-01)。

验证:
- 同一 factory + configure 回调,按 route_groups 分别装配出普通 app(全 group)
  与 Harness app(仅数据面),数据面行为一致,控制面只进普通 app。
- 不同 app 实例的 per-app state 相互隔离(runner / stream_registry 不共享)。
- 薄兼容壳:模块级 ``app`` 由 factory 产出,``set_runner`` 写入 app.state.runtime。
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from ksadk.server.app import _configure_runtime_app
from ksadk.server.factory import (
    ALL_GROUPS,
    CONTROL_PLANE_GROUPS,
    DATA_PLANE_GROUPS,
    RuntimeAppConfig,
    create_runtime_app,
)

# ksadk.server 包的 __init__ 把 FastAPI 实例绑到属性 ``app`` 上,遮蔽了子模块;
# 用 importlib 拿到真正的 ksadk.server.app 模块(与其它测试一致)。
server_app_module = importlib.import_module("ksadk.server.app")


def _paths(app) -> set[str]:
    # 兼容 fastapi >= 0.139:``include_router`` 产物是懒加载 ``_IncludedRouter``,
    # 其真实路由在 ``original_router.routes``;旧版本则是直接的 APIRoute(.path)。
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


def _make(route_groups) -> object:
    return create_runtime_app(
        RuntimeAppConfig(route_groups=set(route_groups)),
        _configure_runtime_app,
    )


def test_route_group_sets_are_disjoint_and_complete():
    assert DATA_PLANE_GROUPS.isdisjoint(CONTROL_PLANE_GROUPS)
    assert DATA_PLANE_GROUPS | CONTROL_PLANE_GROUPS == ALL_GROUPS


def test_normal_app_includes_control_plane():
    paths = _paths(_make(ALL_GROUPS))
    # 数据面
    assert "/health" in paths
    assert "/agentengine/api/v1/RunAgent" in paths
    assert "/agentengine/api/v1/SubscribeRunEvents" in paths
    assert "/v1/chat/completions" in paths
    # 控制面(普通 app 有)
    assert "/agentengine/api/v1/CancelRun" in paths
    assert "/agentengine/api/v1/ResumeRun" in paths
    assert "/agentengine/api/v1/GetCheckpointResumePreview" in paths
    assert "/builder/save" in paths
    assert "/traces" in paths


def test_harness_app_excludes_control_plane():
    paths = _paths(_make(DATA_PLANE_GROUPS))
    # 数据面保留
    assert "/health" in paths
    assert "/agentengine/api/v1/RunAgent" in paths
    assert "/agentengine/api/v1/SubscribeRunEvents" in paths
    assert "/v1/chat/completions" in paths
    assert "/agentengine/api/v1/ListWorkspaceFiles" in paths
    # 控制面剔除
    assert "/agentengine/api/v1/CancelRun" not in paths
    assert "/agentengine/api/v1/ResumeRun" not in paths
    assert "/agentengine/api/v1/GetCheckpointResumePreview" not in paths
    assert "/builder/save" not in paths
    assert "/builder/app/{app_name}" not in paths
    assert "/traces" not in paths


def test_apps_have_isolated_per_app_state():
    app_a = _make(ALL_GROUPS)
    app_b = _make(ALL_GROUPS)

    assert app_a.state.runtime is not app_b.state.runtime
    assert app_a.state.runtime.stream_registry is not app_b.state.runtime.stream_registry

    sentinel = object()
    app_a.state.runtime.runner = sentinel
    assert app_b.state.runtime.runner is None
    assert app_a.state.runtime.runner is sentinel


def test_health_consistent_across_normal_and_harness():
    for groups in (ALL_GROUPS, DATA_PLANE_GROUPS):
        client = TestClient(_make(groups))
        response = client.get("/health")
        assert response.status_code == 200


def test_compat_shell_app_and_set_runner():
    # 模块级 app 由 factory 产出,且为完整普通 app(含控制面)。
    paths = _paths(server_app_module.app)
    assert "/agentengine/api/v1/CancelRun" in paths
    # set_runner 写入该 app 的 per-app state(且重置 loaded 标记)。
    sentinel = object()
    server_app_module.set_runner(sentinel)  # type: ignore[arg-type]
    try:
        assert server_app_module.app.state.runtime.runner is sentinel
        assert server_app_module.app.state.runtime.runner_loaded is False
    finally:
        server_app_module.app.state.runtime.runner = None


def test_compat_shell_set_runner_preserves_preloaded_state():
    sentinel = object()
    server_app_module.set_runner(sentinel, loaded=True)  # type: ignore[arg-type]
    try:
        assert server_app_module.app.state.runtime.runner is sentinel
        assert server_app_module.app.state.runtime.runner_loaded is True
    finally:
        server_app_module.app.state.runtime.runner = None
        server_app_module.app.state.runtime.runner_loaded = False
