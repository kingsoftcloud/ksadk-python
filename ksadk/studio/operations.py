"""Persistent asynchronous operations with idempotency and resumable events."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, cast
from uuid import uuid4

from pydantic import ValidationError

from ksadk.studio.contracts import (
    Operation,
    OperationEvent,
    OperationKind,
    OperationStatus,
)
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.workspace import Workspace


class OperationManager:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._tasks: dict[str, asyncio.Task] = {}
        self._recover_interrupted()

    def submit(
        self,
        *,
        kind: OperationKind,
        resource_id: str,
        idempotency_key: str,
        runner: Callable[[], Awaitable[object]],
    ) -> Operation:
        existing = self._find_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
        operation = Operation(
            id=f"op_{uuid4().hex}",
            kind=kind,
            resource_id=resource_id,
        )
        self._write(operation, [], idempotency_key)
        self.append(operation.id, "operation.queued", {"kind": kind})
        task = asyncio.create_task(self._run(operation.id, runner))
        self._tasks[operation.id] = task

        def remove_completed(_task: asyncio.Task, op_id: str = operation.id) -> None:
            self._tasks.pop(op_id, None)

        task.add_done_callback(remove_completed)
        return operation

    async def _run(
        self,
        operation_id: str,
        runner: Callable[[], Awaitable[object]],
    ) -> None:
        operation = self.get(operation_id)
        operation.status = OperationStatus.RUNNING
        self._save_record(operation)
        self.append(operation_id, "operation.started", {})
        try:
            result = await runner()
            result_id = getattr(result, "id", None)
            if result_id:
                operation.resource_id = str(result_id)
            operation.status = OperationStatus.SUCCEEDED
            operation.completed_at = datetime.now(timezone.utc)
            self._save_record(operation)
            self.append(
                operation_id,
                "operation.succeeded",
                {"resourceId": operation.resource_id},
            )
        except asyncio.CancelledError:
            operation.status = OperationStatus.CANCELLED
            operation.completed_at = datetime.now(timezone.utc)
            self._save_record(operation)
            self.append(operation_id, "operation.cancelled", {})
        except StudioError as exc:
            operation.status = OperationStatus.FAILED
            operation.error = {
                "code": exc.code,
                "message": exc.message,
                "field": exc.field,
            }
            operation.completed_at = datetime.now(timezone.utc)
            self._save_record(operation)
            self.append(operation_id, "operation.failed", operation.error)
        except Exception:
            operation.status = OperationStatus.FAILED
            operation.error = {
                "code": "INTERNAL_ERROR",
                "message": "本地操作执行失败",
            }
            operation.completed_at = datetime.now(timezone.utc)
            self._save_record(operation)
            self.append(operation_id, "operation.failed", operation.error)

    def get(self, operation_id: str) -> Operation:
        operation, _, _ = self._read(operation_id)
        return operation

    def events(self, operation_id: str, *, after: int = 0) -> list[OperationEvent]:
        _, events, _ = self._read(operation_id)
        return [event for event in events if event.id > after]

    def append(self, operation_id: str, event_type: str, data: dict) -> OperationEvent:
        operation, events, key = self._read(operation_id)
        event = OperationEvent(
            id=len(events) + 1,
            operation_id=operation_id,
            type=event_type,
            data=data,
        )
        events.append(event)
        self._write(operation, events, key)
        return event

    def cancel(self, operation_id: str) -> Operation:
        operation = self.get(operation_id)
        if operation.status in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.INTERRUPTED,
        }:
            return operation
        task = self._tasks.get(operation_id)
        if task is not None:
            task.cancel()
        return operation

    async def wait(self, operation_id: str, *, timeout: float = 30) -> Operation:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            operation = self.get(operation_id)
            if operation.status in {
                OperationStatus.SUCCEEDED,
                OperationStatus.FAILED,
                OperationStatus.CANCELLED,
                OperationStatus.INTERRUPTED,
            }:
                return operation
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(operation_id)
            await asyncio.sleep(0.01)

    def _path(self, operation_id: str) -> Path:
        return self.workspace.resolve(
            Path(".agentkit/operations") / f"{operation_id}.json"
        )

    def _save_record(self, operation: Operation) -> None:
        _, events, key = self._read(operation.id)
        self._write(operation, events, key)

    def _write(
        self,
        operation: Operation,
        events: list[OperationEvent],
        idempotency_key: str,
    ) -> None:
        payload = {
            "operation": operation.model_dump(
                by_alias=True, exclude_none=True, mode="json"
            ),
            "events": [
                event.model_dump(by_alias=True, exclude_none=True, mode="json")
                for event in events
            ],
            "idempotencyKey": idempotency_key,
        }
        self.workspace.atomic_write_text(
            self._path(operation.id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )

    def _read(
        self, operation_id: str
    ) -> tuple[Operation, list[OperationEvent], str]:
        path = self._path(operation_id)
        if not path.is_file():
            raise not_found("operation", operation_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            operation = Operation.model_validate(payload["operation"])
            events = [
                OperationEvent.model_validate(item) for item in payload["events"]
            ]
            return operation, events, str(payload["idempotencyKey"])
        except (OSError, ValueError, KeyError, ValidationError) as exc:
            raise StudioError(
                "OPERATION_RECORD_INVALID",
                "Operation 记录损坏",
                status_code=500,
                details={"id": operation_id},
            ) from exc

    def _find_by_idempotency_key(self, key: str) -> Operation | None:
        directory = self.workspace.resolve(".agentkit/operations")
        for path in directory.glob("op_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("idempotencyKey") == key:
                    return cast(Operation, Operation.model_validate(payload["operation"]))
            except (OSError, ValueError, KeyError, ValidationError):
                continue
        return None

    def _recover_interrupted(self) -> None:
        directory = self.workspace.resolve(".agentkit/operations")
        for path in directory.glob("op_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                operation = Operation.model_validate(payload["operation"])
                events = [
                    OperationEvent.model_validate(item)
                    for item in payload.get("events", [])
                ]
                key = str(payload["idempotencyKey"])
            except (OSError, ValueError, KeyError, ValidationError):
                continue
            if operation.status not in {
                OperationStatus.QUEUED,
                OperationStatus.RUNNING,
            }:
                continue
            operation.status = OperationStatus.INTERRUPTED
            operation.completed_at = datetime.now(timezone.utc)
            events.append(
                OperationEvent(
                    id=len(events) + 1,
                    operation_id=operation.id,
                    type="operation.interrupted",
                    data={"reason": "daemon_restart"},
                )
            )
            self._write(operation, events, key)
