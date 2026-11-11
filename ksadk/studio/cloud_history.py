"""Adapt public history envelopes for Studio's older snake_case REST routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ksadk.studio.errors import StudioError


def _aliases(fields: str) -> dict[str, str]:
    aliases = {}
    for name in fields.split():
        wire = "".join(part.capitalize() for part in name.split("_"))
        aliases[wire] = aliases[wire[0].lower() + wire[1:]] = name
    return aliases


_PAGE_FIELDS = _aliases(
    "session_id messages events latest_seq_id has_more next_cursor history_epoch "
    "snapshot_seq_id cursor_source event_order session_contract_version "
    "cursor_compatible_with_runtime persistence_degraded degraded data_source "
    "total offset limit session_deleted"
)
_ROW_FIELDS = _aliases(
    "event_id message_id session_id invocation_id run_id seq_id start_seq_id "
    "source_session_seq event_type timestamp role content metadata status "
    "response_id trace_id root_span_id attachments thinking tool_events blocks activities"
)


def legacy_cloud_history(payload: dict[str, Any]) -> dict[str, Any]:
    """Rename only page/row protocol keys; nested business JSON stays opaque.

    The shared Web Actions keep the original public wire. This adapter belongs
    exclusively at the older Studio REST/SSE boundary, never in the gateway.
    """
    page = {_PAGE_FIELDS.get(key, key): value for key, value in payload.items()}
    for key in ("messages", "events"):
        rows = page.get(key)
        if isinstance(rows, list):
            page[key] = [
                {_ROW_FIELDS.get(name, name): value for name, value in row.items()}
                if isinstance(row, dict) else row
                for row in rows
            ]
    return page


def cloud_event_sequence(event: dict[str, Any]) -> int:
    payload = event.get("payload")
    for row in (event, payload if isinstance(payload, dict) else {}):
        for key in ("seq_id", "seq", "source_session_seq"):
            if key in row:
                value = row[key]
                if type(value) is int and value > 0:
                    return value
                break
    raise StudioError("CLOUD_HISTORY_PAGE_INVALID", "云端事件缺少有效的序号", status_code=502)


@dataclass
class ForwardCloudHistory:
    """Bounded forward pages for the legacy Studio SSE boundary only."""

    cursor: int
    history_epoch: str | None = None
    snapshot_seq_id: int | None = None
    cursor_source: str | None = None
    deleted: bool = False
    terminal: bool = False
    latest_run_id: str | None = None

    def query(self) -> dict[str, Any]:
        return {
            "after_seq_id": self.cursor,
            "limit": 200,
            "event_order": "session_seq",
            **({"cursor_source": self.cursor_source} if self.cursor_source else {}),
            **({"history_epoch": self.history_epoch} if self.history_epoch is not None else {}),
            **({"snapshot_seq_id": self.snapshot_seq_id}
               if self.snapshot_seq_id is not None else {}),
        }

    def accept(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        page = legacy_cloud_history(payload)
        if page.get("session_deleted"):
            self.deleted = True
            return [], False
        if (page.get("event_order") != "session_seq"
                or page.get("cursor_source") != "runtime"
                or type(page.get("has_more")) is not bool):
            raise StudioError(
                "CLOUD_HISTORY_FORWARD_UNSUPPORTED",
                "云端运行时不支持按会话序号读取事件，请升级运行时",
                status_code=502,
            )
        epoch, snapshot = page.get("history_epoch"), page.get("snapshot_seq_id")
        if not isinstance(epoch, str) or not epoch or type(snapshot) is not int or snapshot < 0:
            raise StudioError("CLOUD_HISTORY_PAGE_INVALID", "云端历史快照无效", status_code=502)
        if (self.history_epoch is not None and epoch != self.history_epoch
                or self.cursor_source is not None and page["cursor_source"] != self.cursor_source
                or self.snapshot_seq_id is not None and snapshot != self.snapshot_seq_id):
            raise StudioError(
                "CLOUD_HISTORY_CHANGED", "云端历史来源或快照已变化，请重新加载会话",
                status_code=409,
            )
        rows, next_cursor = page.get("events"), page.get("next_cursor")
        if (not isinstance(rows, list) or len(rows) > 200
                or any(not isinstance(row, dict) for row in rows)
                or type(next_cursor) is not int):
            raise StudioError("CLOUD_HISTORY_PAGE_INVALID", "云端事件分页无效", status_code=502)
        # The forward acknowledgement selects the oldest unread window. Sorting
        # that bounded window tolerates row order without turning a tail into replay.
        rows = sorted(rows, key=cloud_event_sequence)
        sequences = [cloud_event_sequence(row) for row in rows]
        if (any(seq <= self.cursor or seq > snapshot for seq in sequences)
                or len(set(sequences)) != len(sequences)
                or next_cursor != (sequences[-1] if sequences else self.cursor)
                or page["has_more"] and not rows):
            raise StudioError(
                "CLOUD_HISTORY_PAGE_INVALID", "云端事件游标未按顺序推进", status_code=502,
            )
        self.cursor = next_cursor
        self.history_epoch, self.cursor_source = epoch, page["cursor_source"]
        self.snapshot_seq_id = snapshot if page["has_more"] else None
        return rows, page["has_more"]

    def observe(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
        run_id = event.get("invocation_id") or event.get("run_id") or payload.get("run_id")
        if run_id and run_id != self.latest_run_id:
            self.latest_run_id, self.terminal = str(run_id), False
        kind = str(event.get("event_type") or event.get("eventType")
                   or payload.get("event_type") or payload.get("eventType") or "").lower()
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        status = str(payload.get("status") or content.get("status") or "").lower()
        if kind in {"run.started", "run.start", "run.running"} or (
            kind in {"run_status", "run.status"} and status in {"running", "started", "queued"}
        ):
            self.terminal = False
        elif kind in {
            "run.completed", "run.complete", "run.succeeded", "run.failed",
            "run.cancelled", "run.canceled", "run.expired", "run.error",
        } or (kind in {"run_status", "run.status"} and status in {
            "completed", "complete", "succeeded", "success", "failed", "cancelled",
            "canceled", "expired", "error", "aborted",
        }):
            self.terminal = True
