from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles as StarletteStaticFiles

from ksadk.runtime import RuntimeLaunchContext
from ksadk.scheduler.contracts import (
    ScheduleCommandTemplate,
    ScheduledTask,
    ScheduledTaskTarget,
    ScheduleSpec,
)
from ksadk.studio import api as studio_api
from ksadk.studio.errors import StudioError
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.scheduler_runtime import (
    StudioScheduledRuntimeTarget,
    StudioSchedulerRuntimeError,
)
from ksadk.studio.scheduler_service import StudioSchedulerService
from ksadk.studio.service import StudioService
from ksadk.studio.workspace import Workspace


def _task(task_id: str = "daily-report") -> ScheduledTask:
    future = datetime.now(timezone.utc) + timedelta(days=1)
    return ScheduledTask(
        task_id=task_id,
        target=ScheduledTaskTarget(
            tenant_id="local",
            agent_instance_id="local-agent",
            agent_version_ref="build_123",
            authorization_ref="credential://scheduler-local",
        ),
        schedule=ScheduleSpec(kind="once", at=future),
        command=ScheduleCommandTemplate(payload={"content": "write report"}),
    )


def test_service_persists_crud_and_exposes_local_only_availability(tmp_path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioSchedulerService(workspace)

    created = service.create_task(_task())

    assert created.next_run_at is not None
    assert [task.task_id for task in service.list_tasks()] == ["daily-report"]
    availability = service.availability()
    assert availability["scope"] == "local_studio_process"
    assert availability["alwaysOn"] is False

    updated = service.update_task("daily-report", _task().model_copy(update={"enabled": False}))
    assert updated.enabled is False
    assert service.get_task("daily-report").enabled is False
    service.delete_task("daily-report")
    with pytest.raises(StudioError, match="不存在"):
        service.get_task("daily-report")


@pytest.mark.asyncio
async def test_service_refuses_to_pretend_scheduler_runs_without_kernel(
    tmp_path, monkeypatch
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioSchedulerService(workspace)
    service.create_task(_task())
    monkeypatch.setattr("ksadk.studio.scheduler_service.kernel_route_active", lambda: False)

    with pytest.raises(StudioError, match="不会伪装为已运行") as captured:
        await service.run_now("daily-report")
    assert captured.value.code == "SCHEDULER_RUNTIME_UNAVAILABLE"


@pytest.mark.asyncio
async def test_agent_schedule_resolves_a_trusted_bound_kernel_target(tmp_path, monkeypatch) -> None:
    """The browser never gets to choose tenant, instance, or permit refs."""

    studio = StudioService(tmp_path)
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
        build_id="build-codex-123",
        agent_id="sales-helper",
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            tenant_id="local-tenant",
            agent_instance_id="instance-sales",
        )
    )

    async def ensure_build(_agent_id: str):
        return SimpleNamespace(id="build-codex-123")

    monkeypatch.setattr(studio, "ensure_current_build", ensure_build)
    monkeypatch.setattr(studio, "resolve_run_spec", lambda _build_id: spec)

    async def ensure_runtime(_build_id: str, *, expected_agent_id: str | None = None):
        assert expected_agent_id == "sales-helper"
        return StudioScheduledRuntimeTarget(
            build_id="build-codex-123",
            agent_id="sales-helper",
            tenant_id="local-tenant",
            agent_instance_id="instance-sales",
        )

    monkeypatch.setattr(studio.scheduler_runtimes, "ensure_build", ensure_runtime)
    monkeypatch.setattr(
        studio.scheduler_runtimes,
        "runtime_for_build",
        lambda _build_id: runtime,
    )

    task = await studio.create_agent_schedule(
        "sales-helper",
        display_name="每日销售摘要",
        prompt="生成昨日销售摘要",
        schedule=ScheduleSpec(
            kind="once",
            at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )

    assert task.display_name == "每日销售摘要"
    assert task.target.agent_id == "sales-helper"
    assert task.target.tenant_id == "local-tenant"
    assert task.target.agent_instance_id == "instance-sales"
    assert task.target.agent_version_ref == "build-codex-123"
    assert task.target.authorization_ref == "runtime://local-kernel-ingress"
    assert [item.task_id for item in studio.list_agent_schedules("sales-helper")] == [task.task_id]
    assert studio.list_agent_schedules("other-agent") == []


@pytest.mark.asyncio
async def test_agent_schedule_refuses_an_unbound_studio_build(tmp_path, monkeypatch) -> None:
    studio = StudioService(tmp_path)
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
        build_id="build-codex-123",
        agent_id="sales-helper",
    )

    async def ensure_build(_agent_id: str):
        return SimpleNamespace(id="build-codex-123")

    monkeypatch.setattr(studio, "ensure_current_build", ensure_build)
    monkeypatch.setattr(studio, "resolve_run_spec", lambda _build_id: spec)

    async def reject_runtime(_build_id: str, *, expected_agent_id: str | None = None):
        del expected_agent_id
        raise StudioSchedulerRuntimeError(
            "SCHEDULER_TARGET_UNAVAILABLE",
            "当前 Agent 没有精确绑定的 Scheduler Runtime",
        )

    monkeypatch.setattr(studio.scheduler_runtimes, "ensure_build", reject_runtime)

    with pytest.raises(StudioError, match="精确绑定") as captured:
        await studio.create_agent_schedule(
            "sales-helper",
            display_name="每日销售摘要",
            prompt="生成昨日销售摘要",
            schedule=ScheduleSpec(
                kind="once",
                at=datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    assert captured.value.code == "SCHEDULER_TARGET_UNAVAILABLE"


def test_scheduler_api_exposes_real_crud_and_local_availability(tmp_path, monkeypatch) -> None:
    """Routes work without treating generated front-end assets as test input."""

    monkeypatch.setattr(
        studio_api,
        "StaticFiles",
        lambda **_kwargs: StarletteStaticFiles(directory=tmp_path),
    )
    service = StudioService(tmp_path)
    # The raw /api/v1/schedules surface now rejects a target build that is not a
    # real immutable Build id; this route-level test exercises CRUD plumbing, so
    # admit the fixture build reference without touching the Build store.
    monkeypatch.setattr(service, "validate_schedule_build", lambda *_args, **_kwargs: None)
    app = studio_api.create_studio_app(
        tmp_path,
        service=service,
        security_enabled=False,
    )
    task = _task().model_dump(mode="json", by_alias=True)
    with TestClient(app) as client:
        created = client.post("/api/v1/schedules", json=task)
        assert created.status_code == 201, created.text
        listed = client.get("/api/v1/schedules")
        assert listed.status_code == 200
        assert [item["taskId"] for item in listed.json()["items"]] == ["daily-report"]
        assert listed.json()["availability"]["alwaysOn"] is False
        assert client.delete("/api/v1/schedules/daily-report").status_code == 204
        assert client.get("/api/v1/schedules/daily-report").status_code == 404


def test_agent_scoped_scheduler_api_keeps_the_kernel_target_server_owned(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        studio_api,
        "StaticFiles",
        lambda **_kwargs: StarletteStaticFiles(directory=tmp_path),
    )
    service = StudioService(tmp_path)
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
        build_id="build-codex-123",
        agent_id="sales-helper",
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            tenant_id="local-tenant",
            agent_instance_id="instance-sales",
        )
    )

    async def ensure_build(_agent_id: str):
        return SimpleNamespace(id="build-codex-123")

    monkeypatch.setattr(service, "ensure_current_build", ensure_build)
    monkeypatch.setattr(service, "resolve_run_spec", lambda _build_id: spec)

    async def ensure_runtime(_build_id: str, *, expected_agent_id: str | None = None):
        assert expected_agent_id == "sales-helper"
        return StudioScheduledRuntimeTarget(
            build_id="build-codex-123",
            agent_id="sales-helper",
            tenant_id="local-tenant",
            agent_instance_id="instance-sales",
        )

    monkeypatch.setattr(service.scheduler_runtimes, "ensure_build", ensure_runtime)
    monkeypatch.setattr(
        service.scheduler_runtimes,
        "runtime_for_build",
        lambda _build_id: runtime,
    )
    app = studio_api.create_studio_app(tmp_path, service=service, security_enabled=False)
    payload = {
        "displayName": "每日销售摘要",
        "prompt": "生成昨日销售摘要",
        "schedule": {
            "kind": "once",
            "at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    }
    with TestClient(app) as client:
        created = client.post("/api/v1/agents/sales-helper/schedules", json=payload)
        assert created.status_code == 201, created.text
        task = created.json()
        assert task["target"]["agentId"] == "sales-helper"
        assert task["target"]["agentInstanceId"] == "instance-sales"
        assert "tenantId" not in payload
        task_id = task["taskId"]
        listed = client.get("/api/v1/agents/sales-helper/schedules")
        assert [item["taskId"] for item in listed.json()["items"]] == [task_id]
        assert client.get(
            f"/api/v1/agents/sales-helper/schedules/{task_id}/occurrences"
        ).json() == {"items": []}


def test_validate_schedule_build_rejects_version_name(tmp_path) -> None:
    """A version name (e.g. "v1") is not a Build id and must fail at admission."""
    studio = StudioService(tmp_path)
    with pytest.raises(StudioError) as error:
        studio.validate_schedule_build("v1")
    assert error.value.code == "SCHEDULE_BUILD_UNAVAILABLE"
    assert error.value.status_code == 422
    assert error.value.field == "target.agentVersionRef"


def test_validate_schedule_build_rejects_empty_ref(tmp_path) -> None:
    studio = StudioService(tmp_path)
    with pytest.raises(StudioError) as error:
        studio.validate_schedule_build("  ")
    assert error.value.code == "SCHEDULE_BUILD_REQUIRED"
    assert error.value.status_code == 422


def test_raw_schedule_endpoint_rejects_non_build_version_ref(tmp_path, monkeypatch) -> None:
    """The raw /api/v1/schedules surface must not store a version-name build ref."""
    monkeypatch.setattr(
        studio_api,
        "StaticFiles",
        lambda **_kwargs: StarletteStaticFiles(directory=tmp_path),
    )
    app = studio_api.create_studio_app(
        tmp_path,
        service=StudioService(tmp_path),
        security_enabled=False,
    )
    task = _task().model_dump(mode="json", by_alias=True)
    task["target"]["agentVersionRef"] = "v1"
    with TestClient(app) as client:
        response = client.post("/api/v1/schedules", json=task)
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "SCHEDULE_BUILD_UNAVAILABLE"
        assert "v1" in body["error"]["message"]
        assert client.get("/api/v1/schedules").json()["items"] == []


@pytest.mark.asyncio
async def test_dispatch_preserves_typed_build_unavailable_error(tmp_path, monkeypatch) -> None:
    """A missing build at dispatch time yields SCHEDULER_BUILD_UNAVAILABLE, not DISPATCH_FAILED."""
    from ksadk.sessions.in_memory import InMemorySessionService
    from ksadk.studio.scheduler_runtime import StudioScheduledKernelRegistry

    workspace = Workspace(tmp_path)
    workspace.initialize()

    def resolve_build(_build_id: str) -> StudioRunSpec:
        raise StudioError("BUILD_NOT_FOUND", f"build {_build_id} not found", status_code=404)

    def resolve_adapter_provider(_spec: StudioRunSpec):
        raise AssertionError("adapter should not be requested for an unresolved build")

    registry = StudioScheduledKernelRegistry(
        resolve_build=resolve_build,
        resolve_adapter_provider=resolve_adapter_provider,
        session_service=InMemorySessionService(),
    )
    await registry.start()
    service = StudioSchedulerService(workspace, runtime_registry=registry)

    future = datetime.now(timezone.utc) + timedelta(seconds=1)
    task = ScheduledTask(
        task_id="dispatch-clarity",
        target=ScheduledTaskTarget(
            agent_id="sales-helper",
            tenant_id="local",
            agent_instance_id="studio-schedule-irrelevant",
            agent_version_ref="build_does_not_exist",
            authorization_ref="runtime://local-kernel-ingress",
        ),
        schedule=ScheduleSpec(kind="once", at=future),
        command=ScheduleCommandTemplate(payload={"content": "write report"}),
    )
    service.create_task(task)
    try:
        occurrence = await service.run_now("dispatch-clarity")
    finally:
        await service.stop()
    assert occurrence.state == "failed"
    assert occurrence.error_code == "SCHEDULER_BUILD_UNAVAILABLE"
    assert "build_does_not_exist" in (occurrence.detail or "")
