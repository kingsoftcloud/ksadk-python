"""Persistent local Run and Event store."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from pydantic import ValidationError

from ksadk.studio.contracts import RunEvent, RunRecord
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.workspace import Workspace


class RunEventStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _path(self, run_id: str) -> Path:
        return self.workspace.resolve(Path(".agentkit/runs") / f"{run_id}.json")

    def create(self, record: RunRecord) -> RunRecord:
        path = self._path(record.id)
        if path.exists():
            raise StudioError("RUN_ALREADY_EXISTS", "Run 已存在", status_code=409)
        self._write(record, [])
        return record

    def save(self, record: RunRecord) -> RunRecord:
        _, events = self._read(record.id)
        self._write(record, events)
        return record

    def append(self, run_id: str, event_type: str, data: dict) -> RunEvent:
        record, events = self._read(run_id)
        event = RunEvent(
            id=len(events) + 1,
            run_id=run_id,
            type=event_type,
            data=data,
        )
        events.append(event)
        self._write(record, events)
        return event

    def get(self, run_id: str) -> RunRecord:
        record, _ = self._read(run_id)
        return record

    def events(self, run_id: str, *, after: int = 0) -> list[RunEvent]:
        _, events = self._read(run_id)
        return [event for event in events if event.id > after]

    def list_runs(self, *, session_id: str | None = None) -> List[RunRecord]:
        records: list[RunRecord] = []
        directory = self.workspace.resolve(".agentkit/runs")
        for path in sorted(directory.glob("run_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = RunRecord.model_validate(payload["record"])
            except (OSError, ValueError, KeyError, ValidationError):
                continue
            if session_id and record.session_id != session_id:
                continue
            records.append(record)
        records.sort(
            key=lambda item: item.started_at
            or item.completed_at
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=False,
        )
        return records

    def delete_session(self, session_id: str) -> int:
        """Delete every persisted run that belongs to one local chat session."""

        deleted = 0
        for record in self.list_runs(session_id=session_id):
            path = self._path(record.id)
            if path.is_file():
                path.unlink()
                deleted += 1
        return deleted

    def trace(self, trace_id: str) -> dict:
        for record in self.list_runs():
            if record.trace_id == trace_id:
                return {
                    "traceId": trace_id,
                    "run": record.model_dump(by_alias=True, exclude_none=True, mode="json"),
                    "events": [
                        event.model_dump(by_alias=True, exclude_none=True, mode="json")
                        for event in self.events(record.id)
                    ],
                }
        raise not_found("trace", trace_id)

    def _read(self, run_id: str) -> tuple[RunRecord, List[RunEvent]]:
        path = self._path(run_id)
        if not path.is_file():
            raise not_found("run", run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = RunRecord.model_validate(payload["record"])
            events = [RunEvent.model_validate(item) for item in payload["events"]]
            return record, events
        except (OSError, ValueError, KeyError, ValidationError) as exc:
            raise StudioError(
                "RUN_RECORD_INVALID",
                "Run 记录损坏",
                status_code=500,
                details={"id": run_id},
            ) from exc

    def _write(self, record: RunRecord, events: List[RunEvent]) -> None:
        payload = {
            "record": record.model_dump(by_alias=True, exclude_none=True, mode="json"),
            "events": [
                event.model_dump(by_alias=True, exclude_none=True, mode="json")
                for event in events
            ],
        }
        self.workspace.atomic_write_text(
            self._path(record.id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
