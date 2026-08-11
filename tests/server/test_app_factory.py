"""create_runtime_app factory 一致性与隔离性测试 (goal-01)。

验证:
- 同一 factory + configure 回调,按 route_groups 分别装配出普通 app(全 group)
  与 Harness app(仅数据面),数据面行为一致,控制面只进普通 app。
- 不同 app 实例的 per-app state 相互隔离(executor / stream_registry 不共享)。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import (
    ALL_GROUPS,
    CONTROL_PLANE_GROUPS,
    DATA_PLANE_GROUPS,
    RuntimeAppConfig,
    create_runtime_app,
)


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
        configure_runtime_app,
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
    app_a.state.runtime.executor = sentinel
    assert app_b.state.runtime.executor is None
    assert app_a.state.runtime.executor is sentinel


def test_health_consistent_across_normal_and_harness():
    for groups in (ALL_GROUPS, DATA_PLANE_GROUPS):
        client = TestClient(_make(groups))
        response = client.get("/health")
        assert response.status_code == 200
