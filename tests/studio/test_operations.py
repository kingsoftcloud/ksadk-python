from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ksadk.studio.contracts import OperationKind
from ksadk.studio.operations import OperationManager
from ksadk.studio.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    value = Workspace(tmp_path)
    value.initialize()
    return value


@pytest.mark.asyncio
async def test_operation_runs_persists_events_and_returns_resource_id(workspace):
    manager = OperationManager(workspace)

    class Result:
        id = "build_demo"

    operation = manager.submit(
        kind=OperationKind.BUILD,
        resource_id="demo-agent",
        idempotency_key="demo-r1",
        runner=lambda _operation_id: asyncio.sleep(0, result=Result()),
    )
    completed = await manager.wait(operation.id)

    assert completed.status == "SUCCEEDED"
    assert completed.resource_id == "build_demo"
    assert [event.type for event in manager.events(operation.id)] == [
        "operation.queued",
        "operation.started",
        "operation.succeeded",
    ]


@pytest.mark.asyncio
async def test_operation_idempotency_does_not_execute_twice(workspace):
    manager = OperationManager(workspace)
    calls = 0

    async def runner(_operation_id):
        nonlocal calls
        calls += 1
        return object()

    first = manager.submit(
        kind=OperationKind.BUILD,
        resource_id="demo-agent",
        idempotency_key="same-key",
        runner=runner,
    )
    second = manager.submit(
        kind=OperationKind.BUILD,
        resource_id="demo-agent",
        idempotency_key="same-key",
        runner=runner,
    )
    await manager.wait(first.id)

    assert first.id == second.id
    assert calls == 1


@pytest.mark.asyncio
async def test_operation_cancel_is_terminal_and_idempotent(workspace):
    manager = OperationManager(workspace)
    gate = asyncio.Event()
    operation = manager.submit(
        kind=OperationKind.RUN,
        resource_id="build_demo",
        idempotency_key="cancel-key",
        runner=lambda _operation_id: gate.wait(),
    )
    await asyncio.sleep(0)
    manager.cancel(operation.id)
    completed = await manager.wait(operation.id)

    assert completed.status == "CANCELLED"
    assert manager.cancel(operation.id).status == "CANCELLED"


@pytest.mark.asyncio
async def test_operation_cancel_before_task_start_is_terminal(workspace):
    manager = OperationManager(workspace)
    gate = asyncio.Event()
    operation = manager.submit(
        kind=OperationKind.RUN,
        resource_id="build_demo",
        idempotency_key="cancel-before-start",
        runner=lambda _operation_id: gate.wait(),
    )

    cancelled = manager.cancel(operation.id)

    assert cancelled.status == "CANCELLED"
    assert [event.type for event in manager.events(operation.id)] == [
        "operation.queued",
        "operation.cancelled",
    ]


@pytest.mark.asyncio
async def test_operation_passes_id_to_runner(workspace):
    manager = OperationManager(workspace)
    received: list[str] = []

    async def runner(operation_id: str):
        received.append(operation_id)

    operation = manager.submit(
        kind=OperationKind.EVALUATION,
        resource_id="eval_demo",
        idempotency_key="runner-operation-id",
        runner=runner,
    )
    await manager.wait(operation.id)

    assert received == [operation.id]


@pytest.mark.asyncio
async def test_operation_list_filters_kind_and_persists_metadata(workspace):
    manager = OperationManager(workspace)
    evaluation = manager.submit(
        kind=OperationKind.EVALUATION,
        resource_id="eval_demo",
        idempotency_key="evaluation-list",
        metadata={"evalset": {"name": "smoke", "caseCount": 2}},
        runner=lambda _operation_id: asyncio.sleep(0),
    )
    build = manager.submit(
        kind=OperationKind.BUILD,
        resource_id="build_demo",
        idempotency_key="build-list",
        runner=lambda _operation_id: asyncio.sleep(0),
    )
    await manager.wait(evaluation.id)
    await manager.wait(build.id)

    listed = manager.list(kind=OperationKind.EVALUATION)

    assert [operation.id for operation in listed] == [evaluation.id]
    assert listed[0].metadata == {"evalset": {"name": "smoke", "caseCount": 2}}
    assert OperationManager(workspace).get(evaluation.id).metadata == listed[0].metadata


@pytest.mark.asyncio
async def test_restart_marks_non_terminal_operation_interrupted(workspace):
    first = OperationManager(workspace)
    gate = asyncio.Event()
    operation = first.submit(
        kind=OperationKind.BUILD,
        resource_id="demo-agent",
        idempotency_key="restart-key",
        runner=lambda _operation_id: gate.wait(),
    )
    await asyncio.sleep(0)

    recovered = OperationManager(workspace)

    assert recovered.get(operation.id).status == "INTERRUPTED"
    assert recovered.events(operation.id)[-1].type == "operation.interrupted"
    first.cancel(operation.id)
