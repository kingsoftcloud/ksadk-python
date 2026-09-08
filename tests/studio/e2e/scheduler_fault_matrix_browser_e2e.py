"""Browser RC gate for Scheduler Lite failure and recovery diagnostics.

The fixture replaces only the external execution boundary.  Production
``SchedulerEngine`` and ``SchedulerSQLiteStore`` create every occurrence.  A
real Chromium page then proves failure, timeout, restart recovery, misfire and
concurrency facts are visible, and reloads them after both a page refresh and
a Studio process restart.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.kernel.contracts import SessionEventEnvelope
from ksadk.kernel.ingress import clear_agent_kernel
from ksadk.scheduler import SchedulerEngine, SchedulerSQLiteStore
from ksadk.scheduler.contracts import (
    ScheduleCommandTemplate,
    ScheduledTask,
    ScheduledTaskTarget,
    ScheduleSpec,
)
from ksadk.scheduler.engine import SchedulerDispatchError, SchedulerDispatchReceipt
from ksadk.studio.service import StudioService

AGENT_ID = "scheduler-fault-agent"


def _event(
    occurrence,
    *,
    seq: int,
    event_type: str,
    run_id: str,
    causation_id: str | None = None,
    payload: dict | None = None,
) -> SessionEventEnvelope:
    family = "control" if event_type.startswith("control.") else "runtime"
    return SessionEventEnvelope(
        event_id=uuid4(),
        session_id=occurrence.session_id,
        seq=seq,
        timestamp=datetime.now(timezone.utc).isoformat(),
        family=family,
        family_version=1 if family == "control" else 2,
        event_type=event_type,
        run_id=run_id,
        causation_id=causation_id,
        payload=payload or {},
    )


class _FaultMatrixDispatcher:
    """Deterministic external boundary with canonical runtime event replay."""

    def __init__(self, *, recover_restart: bool = False) -> None:
        self.recover_restart = recover_restart
        self.replayed: set[str] = set()

    async def dispatch(self, task: ScheduledTask, occurrence) -> SchedulerDispatchReceipt:
        if task.task_id == "failure-case":
            raise SchedulerDispatchError(
                "PLUGIN_EXECUTION_FAILED",
                "provider process exited with status 17",
            )
        return SchedulerDispatchReceipt(
            f"command-{task.task_id}-{occurrence.occurrence_id[-6:]}",
            accepted_seq=10,
        )

    async def read_events(self, occurrence):
        if occurrence.occurrence_id in self.replayed:
            return ()
        if occurrence.task_id == "timeout-case":
            run_id = "run-timeout"
            terminal_type = "run.failed"
            terminal_payload = {
                "error": {
                    "code": "RUNTIME_TIMEOUT",
                    "message": "execution exceeded the 30 second deadline",
                }
            }
        elif occurrence.task_id == "restart-case" and self.recover_restart:
            run_id = "run-after-restart"
            terminal_type = "run.completed"
            terminal_payload = {}
        else:
            return ()
        self.replayed.add(occurrence.occurrence_id)
        return (
            (
                11,
                _event(
                    occurrence,
                    seq=11,
                    event_type="control.run_transition",
                    run_id=run_id,
                    causation_id=occurrence.command_id,
                    payload={"state": "running", "run_id": run_id},
                ),
            ),
            (
                12,
                _event(
                    occurrence,
                    seq=12,
                    event_type=terminal_type,
                    run_id=run_id,
                    payload=terminal_payload,
                ),
            ),
        )


def _task(
    task_id: str,
    display_name: str,
    *,
    schedule: ScheduleSpec,
    next_run_at: datetime,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        display_name=display_name,
        target=ScheduledTaskTarget(
            agent_id=AGENT_ID,
            tenant_id="local",
            agent_instance_id="scheduler-fault-instance",
            agent_version_ref="build_scheduler_fault_matrix",
            authorization_ref="credential://scheduler-local",
        ),
        schedule=schedule,
        command=ScheduleCommandTemplate(payload={"content": f"execute {display_name}"}),
        next_run_at=next_run_at,
        created_at=next_run_at,
        updated_at=next_run_at,
    )


async def _prepare_matrix(workspace: Path) -> None:
    store = SchedulerSQLiteStore(workspace / ".agentkit/scheduler/scheduler.sqlite3")
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    dispatcher = _FaultMatrixDispatcher()

    failure_at = base
    store.put_task(
        _task(
            "failure-case",
            "故障矩阵 · 提交失败",
            schedule=ScheduleSpec(kind="once", at=failure_at, misfire_policy="run_once"),
            next_run_at=failure_at,
        )
    )
    failure = await SchedulerEngine(
        store,
        dispatcher,
        owner_id="fault-matrix",
        clock=lambda: failure_at,
    ).tick()
    assert [(item.state, item.error_code) for item in failure] == [
        ("failed", "PLUGIN_EXECUTION_FAILED")
    ]

    timeout_at = base + timedelta(seconds=1)
    store.put_task(
        _task(
            "timeout-case",
            "故障矩阵 · 执行超时",
            schedule=ScheduleSpec(kind="once", at=timeout_at, misfire_policy="run_once"),
            next_run_at=timeout_at,
        )
    )
    timeout_engine = SchedulerEngine(
        store,
        dispatcher,
        owner_id="fault-matrix",
        clock=lambda: timeout_at,
    )
    assert (await timeout_engine.tick())[0].state == "accepted"
    timeout = await timeout_engine.reconcile()
    timeout_facts = [(item.state, item.error_code) for item in timeout]
    assert timeout_facts == [("failed", "RUNTIME_TIMEOUT")], timeout_facts

    restart_at = base + timedelta(seconds=2)
    store.put_task(
        _task(
            "restart-case",
            "故障矩阵 · 重启恢复",
            schedule=ScheduleSpec(kind="once", at=restart_at, misfire_policy="run_once"),
            next_run_at=restart_at,
        )
    )
    first_process = SchedulerEngine(
        store,
        dispatcher,
        owner_id="fault-matrix",
        clock=lambda: restart_at,
    )
    assert (await first_process.tick())[0].state == "accepted"
    restarted = SchedulerEngine(
        store,
        _FaultMatrixDispatcher(recover_restart=True),
        clock=lambda: restart_at + timedelta(seconds=1),
    )
    recovered = await restarted.reconcile()
    assert [(item.state, item.run_id) for item in recovered] == [
        ("succeeded", "run-after-restart")
    ]

    concurrency_at = base + timedelta(seconds=3)
    concurrency_task = _task(
        "concurrency-case",
        "故障矩阵 · 并发禁止",
        schedule=ScheduleSpec(
            kind="interval",
            every_seconds=60,
            anchor_at=concurrency_at,
            misfire_policy="run_once",
        ),
        next_run_at=concurrency_at,
    )
    store.put_task(concurrency_task)
    concurrency_engine = SchedulerEngine(
        store,
        dispatcher,
        owner_id="fault-matrix",
        clock=lambda: concurrency_at,
    )
    assert (await concurrency_engine.tick())[0].state == "accepted"
    concurrency_engine.clock = lambda: concurrency_at + timedelta(seconds=60)
    skipped = await concurrency_engine.tick()
    assert [(item.state, item.detail) for item in skipped] == [
        ("skipped", "concurrency_forbid_active_occurrence")
    ]
    current, generation = store.get_task("concurrency-case") or (None, None)
    assert current is not None and generation is not None
    store.put_task(current.model_copy(update={"enabled": False}), generation=generation)

    misfire_at = base + timedelta(seconds=4)
    store.put_task(
        _task(
            "misfire-case",
            "故障矩阵 · 错过调度",
            schedule=ScheduleSpec(kind="once", at=misfire_at, misfire_policy="skip"),
            next_run_at=misfire_at,
        )
    )
    misfire = await SchedulerEngine(
        store,
        dispatcher,
        owner_id="fault-matrix",
        clock=lambda: misfire_at + timedelta(hours=2),
    ).tick()
    assert [(item.state, item.detail) for item in misfire] == [
        ("skipped", "misfire_skipped")
    ]


def _assert_fault_matrix(page: Page, base_url: str) -> None:
    # Studio deliberately performs optional capability discovery during startup.
    # A missing/slow plugin host must not make Scheduler readiness depend on the
    # browser reaching a global network-idle state; the page's own heading and
    # durable history are the product-level readiness signals.
    page.goto(f"{base_url}/#/automations", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="自动化 / 定时任务")).to_be_visible()
    page.get_by_role("tab", name="执行记录", exact=True).click()

    history = page.locator(".automation-history")
    expect(history.locator(".automation-occurrence-card")).to_have_count(6)

    failure = history.locator(".automation-occurrence-card").filter(
        has_text="故障矩阵 · 提交失败"
    )
    expect(failure).to_contain_text("失败")
    expect(failure).to_contain_text("插件执行失败 · PLUGIN_EXECUTION_FAILED")
    expect(failure).to_contain_text("provider process exited with status 17")

    timeout = history.locator(".automation-occurrence-card").filter(
        has_text="故障矩阵 · 执行超时"
    )
    expect(timeout).to_contain_text("失败")
    expect(timeout).to_contain_text("执行超时 · RUNTIME_TIMEOUT")
    expect(timeout).to_contain_text("execution exceeded the 30 second deadline")

    recovery = history.locator(".automation-occurrence-card").filter(
        has_text="故障矩阵 · 重启恢复"
    )
    expect(recovery).to_contain_text("成功")
    expect(recovery).to_contain_text("运行时已确认完成")
    expect(recovery.locator(".automation-timeline li")).to_have_count(4)
    expect(recovery.locator(".automation-timeline")).to_contain_text("已接收")
    expect(recovery.locator(".automation-timeline")).to_contain_text("运行中")

    misfire = history.locator(".automation-occurrence-card").filter(
        has_text="故障矩阵 · 错过调度"
    )
    expect(misfire).to_contain_text("已跳过")
    expect(misfire).to_contain_text("错过计划时间，已按策略跳过")

    concurrency = history.locator(".automation-occurrence-card").filter(
        has_text="已有执行未结束，本次已跳过"
    )
    expect(concurrency).to_have_count(1)
    expect(concurrency).to_contain_text("故障矩阵 · 并发禁止")
    expect(concurrency).to_contain_text("已跳过")

    # A normal page refresh must reconstruct all facts from HTTP + SQLite.
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("tab", name="执行记录", exact=True).click()
    expect(page.locator(".automation-occurrence-card")).to_have_count(6)
    expect(page.get_by_text("执行超时 · RUNTIME_TIMEOUT", exact=True)).to_be_visible()


def main() -> None:
    with TemporaryDirectory(prefix="ksadk-scheduler-fault-browser-") as temp_dir:
        workspace = Path(temp_dir)
        asyncio.run(_prepare_matrix(workspace))
        clear_agent_kernel()

        with (
            patch.dict(
                os.environ,
                {"KSADK_AGENT_KERNEL": "0", "AGENT_KERNEL_ENABLED": "0"},
            ),
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                with studio_server(workspace, service=StudioService(workspace)) as base_url:
                    _assert_fault_matrix(page, base_url)

                # Recreate the complete Studio service over the same workspace.
                # The second browser pass is evidence of process-level replay,
                # not retained React state or a fixture HTTP response.
                with studio_server(workspace, service=StudioService(workspace)) as base_url:
                    _assert_fault_matrix(page, base_url)

                assert page_errors == [], f"Uncaught React page errors: {page_errors}"
            finally:
                browser.close()


if __name__ == "__main__":
    main()
