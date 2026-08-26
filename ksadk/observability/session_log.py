"""Versioned, fixed-watermark JSONL exports for canonical RuntimeEvents.

New exports use the schema-v2 RuntimeEvent envelope.  The legacy v1 log is
still accepted by :func:`verify_session_log` so existing diagnostic files stay
readable, but a v2 Store must never be coerced back into a v1 write model just
to produce an export.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from ksadk.events.canonical import dump_runtime_event, parse_runtime_event
from ksadk.events.store import RuntimeEventStore
from ksadk.events.v1_compat import EventTypeV1, RuntimeEventV1
from ksadk.sessions.base import BaseSessionService

SESSION_LOG_SCHEMA = "ksadk.session-log/v2"
_SESSION_LOG_VERSION = 2
_LEGACY_SESSION_LOG_SCHEMA = "ksadk.session-log/v1"
_LEGACY_SESSION_LOG_VERSION = 1
_PAGE_SIZE = 500
_LEGACY_PACKED_EVENT_TYPES = {
    EventTypeV1.TEXT_DELTA: "text-chunks",
    EventTypeV1.REASONING_DELTA: "reasoning-chunks",
}
_LEGACY_PACKED_RECORD_TYPES = {value: key for key, value in _LEGACY_PACKED_EVENT_TYPES.items()}


class SessionLogError(ValueError):
    """A stable Session Log export or validation failure."""


@dataclass(frozen=True)
class SessionLogResult:
    path: Path
    event_count: int
    first_seq_id: int | None
    last_seq_id: int | None
    exported_through_seq_id: int | None


def _raise(code: str, message: str) -> None:
    raise SessionLogError(f"{code}: {message}")


def _write_json_line(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    )


def _legacy_packed_base(event: RuntimeEventV1) -> dict[str, Any]:
    value = event.to_dict()
    for key in ("event_id", "seq_id", "timestamp"):
        value.pop(key)
    payload = dict(value["payload"])
    payload.pop("text")
    value["payload"] = payload
    return value


def _write_legacy_event_run(stream: TextIO, events: list[RuntimeEventV1]) -> None:
    if len(events) < 3:
        for event in events:
            _write_json_line(stream, event.to_dict())
        return
    _write_json_line(
        stream,
        {
            "type": _LEGACY_PACKED_EVENT_TYPES[events[0].event_type],
            "seq0": events[0].seq_id,
            "data": {
                "base": _legacy_packed_base(events[0]),
                "event_ids": [event.event_id for event in events],
                "timestamps": [event.timestamp for event in events],
                "texts": [event.payload["text"] for event in events],
            },
        },
    )


def _same_legacy_event_run(events: list[RuntimeEventV1], event: RuntimeEventV1) -> bool:
    return (
        bool(events)
        and event.event_type == events[0].event_type
        and event.seq_id == events[-1].seq_id + 1
        and _legacy_packed_base(event) == _legacy_packed_base(events[0])
    )


async def export_session_log(
    session_service: BaseSessionService,
    session_id: str,
    target: Path | str,
    *,
    invocation_id: str | None = None,
) -> SessionLogResult:
    """Export committed RuntimeEvents through a fixed session cursor."""
    session = await session_service.get_session_metadata(session_id)
    if session is None:
        _raise("SESSION_LOG_SESSION_NOT_FOUND", f"session {session_id!r} not found")

    target_path = Path(target)
    if target_path.exists():
        _raise("SESSION_LOG_TARGET_EXISTS", f"target {target_path} already exists")

    store = RuntimeEventStore(session_service)
    tail = await store.list(session_id, limit=1)
    cutoff = tail[-1].seq if tail else None
    header: dict[str, Any] = {
        "type": "session",
        "schema": SESSION_LOG_SCHEMA,
        "version": _SESSION_LOG_VERSION,
        "session_id": session.id,
        "agent_id": session.agent_id,
        "user_id": session.user_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "exported_through_seq_id": cutoff,
        "event_schema_version": 2,
    }
    if invocation_id is not None:
        header["invocation_id"] = invocation_id

    temporary_path: Path | None = None
    published = False
    event_count = 0
    first_seq_id: int | None = None
    last_seq_id: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=".session-log-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            _write_json_line(stream, header)
            cursor = 0
            while cutoff is not None and cursor < cutoff:
                window_end = min(cursor + _PAGE_SIZE, cutoff)
                events = await store.page(
                    session_id,
                    after_seq=cursor,
                    before_seq=window_end + 1,
                    limit=_PAGE_SIZE,
                )
                for event in events:
                    if invocation_id is not None and event.run_id != invocation_id:
                        continue
                    # A v2 fact is the durable source of truth.  The v1
                    # packed delta format cannot losslessly encode all v2
                    # item operations, so v2 logs retain one canonical event
                    # per row instead of silently projecting/dropping facts.
                    _write_json_line(stream, dump_runtime_event(event))
                    event_count += 1
                    first_seq_id = first_seq_id or event.seq
                    last_seq_id = event.seq
                cursor = window_end
            stream.flush()
            os.fsync(stream.fileno())

        try:
            os.link(temporary_path, target_path)
            published = True
        except FileExistsError as exc:
            raise SessionLogError(
                f"SESSION_LOG_TARGET_EXISTS: target {target_path} already exists"
            ) from exc
        except OSError as exc:
            raise SessionLogError(
                "SESSION_LOG_ATOMIC_PUBLISH_UNSUPPORTED: "
                f"cannot atomically publish {target_path}"
            ) from exc

        directory_fd = os.open(target_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except SessionLogError:
        if published:
            target_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if published:
            target_path.unlink(missing_ok=True)
        raise SessionLogError(f"SESSION_LOG_WRITE_FAILED: {target_path}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return SessionLogResult(
        path=target_path,
        event_count=event_count,
        first_seq_id=first_seq_id,
        last_seq_id=last_seq_id,
        exported_through_seq_id=cutoff,
    )


def _read_json_line(raw: str, line_number: int) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SessionLogError(f"SESSION_LOG_INVALID: line {line_number} is not valid JSON") from exc
    if not isinstance(value, dict):
        _raise("SESSION_LOG_INVALID", f"line {line_number} must be an object")
    return value


def _legacy_events_from_record(
    value: dict[str, Any], line_number: int, *, allow_packed: bool
) -> list[RuntimeEventV1]:
    record_type = value.get("type")
    if record_type not in _LEGACY_PACKED_RECORD_TYPES:
        try:
            return [RuntimeEventV1.from_dict(value)]
        except (TypeError, ValueError) as exc:
            raise SessionLogError(
                f"SESSION_LOG_INVALID: line {line_number} is not a RuntimeEvent"
            ) from exc
    if not allow_packed:
        _raise("SESSION_LOG_INVALID", f"line {line_number} uses packed rows in v1")

    seq0 = value.get("seq0")
    data = value.get("data")
    if isinstance(seq0, bool) or not isinstance(seq0, int) or seq0 < 0:
        _raise("SESSION_LOG_INVALID", f"line {line_number} packed seq0 is invalid")
    if not isinstance(data, dict) or not isinstance(data.get("base"), dict):
        _raise("SESSION_LOG_INVALID", f"line {line_number} packed data is invalid")
    event_ids = data.get("event_ids")
    timestamps = data.get("timestamps")
    texts = data.get("texts")
    if not all(isinstance(items, list) for items in (event_ids, timestamps, texts)):
        _raise("SESSION_LOG_INVALID", f"line {line_number} packed arrays are invalid")
    if len(event_ids) < 3 or len(event_ids) != len(timestamps) or len(event_ids) != len(texts):
        _raise("SESSION_LOG_INVALID", f"line {line_number} packed arrays do not align")

    base = dict(data["base"])
    expected_event_type = _LEGACY_PACKED_RECORD_TYPES[record_type]
    if base.get("event_type") != expected_event_type:
        _raise("SESSION_LOG_INVALID", f"line {line_number} packed event type does not match")
    payload = base.get("payload")
    if not isinstance(payload, dict) or "text" in payload:
        _raise("SESSION_LOG_INVALID", f"line {line_number} packed payload is invalid")

    events: list[RuntimeEventV1] = []
    for index, (event_id, timestamp, text) in enumerate(
        zip(event_ids, timestamps, texts, strict=True)
    ):
        value = {
            **base,
            "event_id": event_id,
            "seq_id": seq0 + index,
            "timestamp": timestamp,
            "payload": {**payload, "text": text},
        }
        try:
            events.append(RuntimeEventV1.from_dict(value))
        except (TypeError, ValueError) as exc:
            raise SessionLogError(
                f"SESSION_LOG_INVALID: line {line_number} contains an invalid packed event"
            ) from exc
    return events


def _validate_header(
    header: dict[str, Any], *, schema: str, version: int
) -> tuple[int | None, str | None]:
    if header.get("type") != "session":
        _raise("SESSION_LOG_INVALID", "first line must be a session header")
    if header.get("schema") != schema or header.get("version") != version:
        _raise("SESSION_LOG_INVALID", "unsupported schema")
    session_id = header.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        _raise("SESSION_LOG_INVALID", "header session id is required")
    cutoff = header.get("exported_through_seq_id")
    if cutoff is not None and (
        isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff < 0
    ):
        _raise("SESSION_LOG_INVALID", "exported watermark must be null or non-negative")
    return cutoff, header.get("invocation_id")


def _finish_verification(
    *,
    source: Path,
    event_count: int,
    first_seq_id: int | None,
    last_seq_id: int | None,
    cutoff: int | None,
    filtered: bool,
) -> SessionLogResult:
    if not filtered:
        if cutoff is None and event_count:
            _raise("SESSION_LOG_INVALID", "empty watermark cannot contain events")
        if cutoff is not None and last_seq_id != cutoff:
            _raise("SESSION_LOG_INVALID", "full session must end at exported watermark")
    return SessionLogResult(
        path=source,
        event_count=event_count,
        first_seq_id=first_seq_id,
        last_seq_id=last_seq_id,
        exported_through_seq_id=cutoff,
    )


def _verify_v2(stream: TextIO, *, source: Path, header: dict[str, Any]) -> SessionLogResult:
    cutoff, invocation_id = _validate_header(
        header, schema=SESSION_LOG_SCHEMA, version=_SESSION_LOG_VERSION
    )
    filtered = invocation_id is not None
    event_count = 0
    first_seq_id: int | None = None
    last_seq_id: int | None = None
    for line_number, raw in enumerate(stream, start=2):
        if not raw.strip():
            _raise("SESSION_LOG_INVALID", f"line {line_number} is empty")
        try:
            event = parse_runtime_event(_read_json_line(raw, line_number))
        except (TypeError, ValueError) as exc:
            raise SessionLogError(
                f"SESSION_LOG_INVALID: line {line_number} is not a RuntimeEvent/v2"
            ) from exc
        if filtered and event.run_id != invocation_id:
            _raise("SESSION_LOG_INVALID", f"line {line_number} run id does not match")
        if last_seq_id is not None and event.seq <= last_seq_id:
            _raise("SESSION_LOG_INVALID", "event seq must be strictly increasing")
        if cutoff is None or event.seq > cutoff:
            _raise("SESSION_LOG_INVALID", "event seq exceeds exported watermark")
        if not filtered:
            expected = 1 if last_seq_id is None else last_seq_id + 1
            if event.seq != expected:
                _raise("SESSION_LOG_INVALID", "full session seq must be continuous")
        event_count += 1
        first_seq_id = first_seq_id or event.seq
        last_seq_id = event.seq
    return _finish_verification(
        source=source,
        event_count=event_count,
        first_seq_id=first_seq_id,
        last_seq_id=last_seq_id,
        cutoff=cutoff,
        filtered=filtered,
    )


def _verify_v1(stream: TextIO, *, source: Path, header: dict[str, Any]) -> SessionLogResult:
    cutoff, invocation_id = _validate_header(
        header, schema=_LEGACY_SESSION_LOG_SCHEMA, version=_LEGACY_SESSION_LOG_VERSION
    )
    session_id = str(header["session_id"])
    filtered = invocation_id is not None
    event_count = 0
    first_seq_id: int | None = None
    last_seq_id: int | None = None
    for line_number, raw in enumerate(stream, start=2):
        if not raw.strip():
            _raise("SESSION_LOG_INVALID", f"line {line_number} is empty")
        value = _read_json_line(raw, line_number)
        for event in _legacy_events_from_record(value, line_number, allow_packed=True):
            if event.session_id != session_id:
                _raise("SESSION_LOG_INVALID", f"line {line_number} session id does not match")
            if filtered and event.invocation_id != invocation_id:
                _raise("SESSION_LOG_INVALID", f"line {line_number} invocation id does not match")
            if last_seq_id is not None and event.seq_id <= last_seq_id:
                _raise("SESSION_LOG_INVALID", "event seq_id must be strictly increasing")
            if cutoff is None or event.seq_id > cutoff:
                _raise("SESSION_LOG_INVALID", "event seq_id exceeds exported watermark")
            if not filtered:
                expected = 1 if last_seq_id is None else last_seq_id + 1
                if event.seq_id != expected:
                    _raise("SESSION_LOG_INVALID", "full session seq_id must be continuous")
            event_count += 1
            first_seq_id = first_seq_id or event.seq_id
            last_seq_id = event.seq_id
    return _finish_verification(
        source=source,
        event_count=event_count,
        first_seq_id=first_seq_id,
        last_seq_id=last_seq_id,
        cutoff=cutoff,
        filtered=filtered,
    )


def verify_session_log(path: Path | str) -> SessionLogResult:
    """Stream and validate a v2 Session Log or a legacy v1 diagnostic file."""
    source = Path(path)
    try:
        stream = source.open(encoding="utf-8")
    except OSError as exc:
        raise SessionLogError(f"SESSION_LOG_READ_FAILED: {source}") from exc
    with stream:
        first_line = stream.readline()
        if not first_line:
            _raise("SESSION_LOG_INVALID", "missing session header")
        header = _read_json_line(first_line, 1)
        if header.get("schema") == SESSION_LOG_SCHEMA:
            return _verify_v2(stream, source=source, header=header)
        if header.get("schema") == _LEGACY_SESSION_LOG_SCHEMA:
            return _verify_v1(stream, source=source, header=header)
        _raise("SESSION_LOG_INVALID", "unsupported schema")


__all__ = [
    "SESSION_LOG_SCHEMA",
    "SessionLogError",
    "SessionLogResult",
    "export_session_log",
    "verify_session_log",
]
