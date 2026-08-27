"""收口 4：Local Deployment 真实启动 Runtime 子进程 + HTTP Health Check。

不再只是进程内对象与配置级检查：deploy(launch_process=True) spawn
``ksadk.harness.runtime_server``（uvicorn），Health Check 是对 /health 的
真实 HTTP 请求；激活取代/回滚/宿主关闭都真实下线进程。
"""

from __future__ import annotations

import sys
import urllib.request

import pytest

from ksadk.harness.lifecycle import (
    LifecycleError,
    LifecycleStatus,
    LocalLifecycleManager,
)

from .test_lifecycle import _revision_payload


def test_deploy_spawns_real_runtime_process_with_http_health():
    manager = LocalLifecycleManager()
    manifest = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    deployment = manager.deploy(
        manifest=manifest,
        revision_payload=_revision_payload(),
        route="studio://p1/local",
        launch_process=True,
    )
    try:
        assert deployment.process_mode
        assert deployment.port is not None and deployment.base_url
        # 真实 HTTP：/health 返回 deploymentId。
        with urllib.request.urlopen(f"{deployment.base_url}/health", timeout=3) as r:
            payload = __import__("json").loads(r.read())
        assert payload["status"] == "ok"
        assert payload["deploymentId"] == deployment.deployment_id
        # /manifest 投影 Build Manifest。
        with urllib.request.urlopen(f"{deployment.base_url}/manifest", timeout=3) as r:
            manifest_payload = __import__("json").loads(r.read())
        assert manifest_payload["contentHash"].startswith("sha256:")
        assert deployment.check_health(), "进程形态 Health Check 走真实 HTTP"
    finally:
        manager.close()
    # 关闭后进程退出，HTTP 不再可达 → Health Check 诚实失败。
    assert deployment.http_health_check() is False
    assert deployment.status == LifecycleStatus.RUNTIME_UNHEALTHY


def test_activate_uses_real_http_and_supersede_terminates_old_process():
    manager = LocalLifecycleManager()
    first = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    first_dep = manager.deploy(
        manifest=first,
        revision_payload=_revision_payload(),
        route="studio://p1/local",
        launch_process=True,
    )
    manager.activate("studio://p1/local")
    assert first_dep.status == LifecycleStatus.ACTIVE
    assert first_dep.process_mode

    second = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@4",
    )
    second_dep = manager.deploy(
        manifest=second,
        revision_payload=_revision_payload(),
        route="studio://p1/local",
        launch_process=True,
    )
    manager.activate("studio://p1/local")
    try:
        assert second_dep.status == LifecycleStatus.ACTIVE
        assert second_dep.check_health()
        # 旧 Active 被取代：进程真实下线。
        assert first_dep.status == LifecycleStatus.SUPERSEDED
        assert first_dep.process is None
        assert not first_dep.http_health_check()
    finally:
        manager.close()


def test_rollback_terminates_runtime_process():
    manager = LocalLifecycleManager()
    manifest = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    manager.deploy(
        manifest=manifest,
        revision_payload=_revision_payload(),
        route="studio://p1/local",
        launch_process=True,
    )
    manager.activate("studio://p1/local")
    rolled = manager.rollback("studio://p1/local")
    assert rolled.status == LifecycleStatus.ROLLED_BACK
    assert rolled.process is None, "回滚必须真实下线 Runtime 子进程"


def test_deploy_fails_honestly_when_process_dies_immediately():
    """进程立即退出 → 诚实 DEPLOY_FAILED，而非伪装部署成功。"""
    manager = LocalLifecycleManager()
    manifest = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    with pytest.raises(LifecycleError, match="health check 未通过"):
        manager.deploy(
            manifest=manifest,
            revision_payload=_revision_payload(),
            route="studio://p1/local",
            launch_process=True,
            health_timeout=5.0,
            server_command=[sys.executable, "-c", "raise SystemExit(3)"],
        )


def test_deploy_fails_honestly_when_health_never_passes():
    """进程活着但不监听端口 → 超时后诚实失败并清理。"""
    manager = LocalLifecycleManager()
    manifest = manager.build(
        revision_payload=_revision_payload(),
        revision_ref="agent-revision://proj-1@3",
    )
    with pytest.raises(LifecycleError, match="health check"):
        manager.deploy(
            manifest=manifest,
            revision_payload=_revision_payload(),
            route="studio://p1/local",
            launch_process=True,
            health_timeout=1.5,
            server_command=[sys.executable, "-c", "import time; time.sleep(60)"],
        )
