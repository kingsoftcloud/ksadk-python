"""Persistent local Run record store and derived trace access."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from ksadk.studio.contracts import RunEvent, RunRecord
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.otel_trace import OtlpTraceStore
from ksadk.studio.workspace import Workspace


class RunEventStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.trace_store = OtlpTraceStore(workspace)

    def _path(self, run_id: str) -> Path:
        return self.workspace.resolve(Path(".agentkit/runs") / f"{run_id}.json")

    def create(self, record: RunRecord) -> RunRecord:
        path = self._path(record.id)
        if path.exists():
            raise StudioError("RUN_ALREADY_EXISTS", "Run 已存在", status_code=409)
        self._write(record)
        return record

    def save(self, record: RunRecord) -> RunRecord:
        self._read(record.id)
        self._write(record)
        return record

    def get(self, run_id: str) -> RunRecord:
        record, _ = self._read(run_id)
        return record

    def list_runs(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[RunRecord]:
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
            if agent_id and record.agent_id != agent_id:
                continue
            records.append(record)
        records.sort(
            key=lambda item: (
                item.started_at or item.completed_at or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=False,
        )
        return records

    def delete_session(self, session_id: str) -> int:
        """Delete every persisted run that belongs to one local chat session."""

        deleted = 0
        for record in self.list_runs(session_id=session_id):
            path = self._path(record.id)
            if path.is_file():
                self.trace_store.delete(record.trace_id, purge=True)
                path.unlink()
                deleted += 1
        return deleted

    def delete_agent(
        self,
        agent_id: str,
        *,
        purge: bool,
        trash_directory: Path | None = None,
    ) -> int:
        """Remove the persisted Runs and Trace events owned by one Agent."""

        deleted = 0
        for record in self.list_runs(agent_id=agent_id):
            path = self._path(record.id)
            if not path.is_file():
                continue
            if purge:
                self.trace_store.delete(record.trace_id, purge=True)
                path.unlink()
            else:
                if trash_directory is None:
                    raise ValueError("recoverable deletion requires a trash directory")
                destination = self.workspace.resolve(trash_directory / "runs" / path.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                self.trace_store.delete(
                    record.trace_id,
                    purge=False,
                    trash_directory=trash_directory,
                )
                shutil.move(str(path), str(destination))
            deleted += 1
        return deleted

    def trace(self, trace_id: str) -> dict:
        return self.trace_store.get_trace_view(trace_id)

    def trace_otlp(self, trace_id: str) -> dict:
        return self.trace_store.get_otlp(trace_id)

    def list_traces(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        return self.trace_store.list_trace_summaries(
            agent_id=agent_id,
            status=status,
            limit=limit,
        )

    def list_traces_page(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        query: str = "",
        limit: int = 50,
        cursor: str | None = None,
        sort: str = "startedAt:desc",
    ) -> dict:
        return self.trace_store.paginate_trace_summaries(
            agent_id=agent_id,
            status=status,
            query=query,
            limit=limit,
            cursor=cursor,
            sort=sort,
        )

    def trace_overview(
        self,
        *,
        range_name: str = "24h",
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        return self.trace_store.trace_overview(
            range_name=range_name,
            agent_id=agent_id,
            status=status,
        )

    def _read(self, run_id: str) -> tuple[RunRecord, list[RunEvent]]:
        path = self._path(run_id)
        if not path.is_file():
            raise not_found("run", run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = RunRecord.model_validate(payload["record"])
            events = [RunEvent.model_validate(item) for item in payload.get("events", [])]
            return record, events
        except (OSError, ValueError, KeyError, ValidationError) as exc:
            raise StudioError(
                "RUN_RECORD_INVALID",
                "Run 记录损坏",
                status_code=500,
                details={"id": run_id},
            ) from exc

    def _write(self, record: RunRecord) -> None:
        payload = {"record": record.model_dump(by_alias=True, exclude_none=True, mode="json")}
        self.workspace.atomic_write_text(
            self._path(record.id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
