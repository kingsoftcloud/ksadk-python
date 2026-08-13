"""Policy-neutral RuntimeEvent projections used by evaluation adapters."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ksadk.events.runtime_event import EventType, RuntimeEvent

from .contracts import DataPolicy, ToolCallEvidence, TraceRef

_SAFE_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class EvidenceStoreError(RuntimeError):
    """Raised when evaluation evidence cannot be safely persisted or read."""


class EvidenceStore:
    """Persist queryable RuntimeEvent evidence below an evaluation report root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def write_trace(
        self,
        run_id: str,
        events: Iterable[RuntimeEvent],
        *,
        policy: DataPolicy = DataPolicy.LOCAL_ONLY,
    ) -> TraceRef:
        event_list = sorted(events, key=lambda item: item.seq_id)
        if not event_list:
            raise EvidenceStoreError("RuntimeEvent evidence must not be empty")
        session_ids = {event.session_id for event in event_list}
        invocation_ids = {event.invocation_id for event in event_list}
        if len(session_ids) != 1 or len(invocation_ids) != 1:
            raise EvidenceStoreError("RuntimeEvent evidence must describe one invocation")
        session_id = next(iter(session_ids))
        invocation_id = next(iter(invocation_ids))
        path = self._trace_path(run_id, session_id, invocation_id)
        payload = {
            "schemaVersion": "ksadk.eval.evidence/v1",
            "runId": run_id,
            "sessionId": session_id,
            "invocationId": invocation_id,
            "dataPolicy": policy.value,
            "seqStart": event_list[0].seq_id,
            "seqEnd": event_list[-1].seq_id,
            "events": [_event_payload(event, policy) for event in event_list],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise EvidenceStoreError("Unable to persist RuntimeEvent evidence") from exc
        return TraceRef(
            run_id=run_id,
            session_id=session_id,
            invocation_id=invocation_id,
            seq_start=event_list[0].seq_id,
            seq_end=event_list[-1].seq_id,
        )

    def read_trace(self, trace_ref: TraceRef) -> dict[str, Any]:
        if not trace_ref.run_id or not trace_ref.session_id or not trace_ref.invocation_id:
            raise EvidenceStoreError("TraceRef does not identify local evaluation evidence")
        path = self._trace_path(
            trace_ref.run_id,
            trace_ref.session_id,
            trace_ref.invocation_id,
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EvidenceStoreError("RuntimeEvent evidence is missing or invalid") from exc
        if (
            payload.get("runId") != trace_ref.run_id
            or payload.get("sessionId") != trace_ref.session_id
            or payload.get("invocationId") != trace_ref.invocation_id
        ):
            raise EvidenceStoreError("RuntimeEvent evidence does not match TraceRef")
        return payload

    def _trace_path(self, run_id: str, session_id: str, invocation_id: str) -> Path:
        for value in (run_id, session_id, invocation_id):
            if not _SAFE_EVIDENCE_ID.fullmatch(value) or value in {".", ".."}:
                raise EvidenceStoreError("Evidence identifiers must be path-safe")
        return self.root / run_id / "evidence" / session_id / f"{invocation_id}.json"


def project_tool_calls(events: Iterable[RuntimeEvent]) -> list[ToolCallEvidence]:
    """Project tool lifecycle events without retaining arguments or results."""

    calls: dict[str, ToolCallEvidence] = {}
    order: list[str] = []
    for event in events:
        if event.event_type not in {EventType.TOOL_CALL_BEGIN, EventType.TOOL_CALL_END}:
            continue
        call_id = str(event.payload.get("call_id") or "").strip()
        name = str(event.payload.get("name") or "").strip()
        if not call_id or not name:
            continue
        current = calls.get(call_id)
        if current is None:
            order.append(call_id)
            current = ToolCallEvidence(
                call_id=call_id,
                name=name,
                status="INCOMPLETE",
                seq_start=(event.seq_id if event.event_type == EventType.TOOL_CALL_BEGIN else None),
            )
        if event.event_type == EventType.TOOL_CALL_END:
            current = current.model_copy(
                update={
                    "name": name,
                    "status": "ERROR" if event.payload.get("error") else "SUCCEEDED",
                    "seq_end": event.seq_id,
                }
            )
        calls[call_id] = current
    return [calls[call_id] for call_id in order]


def _event_payload(event: RuntimeEvent, policy: DataPolicy) -> dict[str, Any]:
    payload = dict(event.payload)
    if policy is DataPolicy.METADATA_ONLY:
        payload = _metadata_payload(event.event_type, payload)
    elif policy is DataPolicy.REDACTED_TRACE:
        payload = _redacted_payload(event.event_type, payload)
    return {
        "schemaVersion": event.schema_version,
        "eventId": event.event_id,
        "eventType": event.event_type,
        "timestamp": event.timestamp,
        "agentId": event.agent_id,
        "userId": event.user_id,
        "sessionId": event.session_id,
        "invocationId": event.invocation_id,
        "seqId": event.seq_id,
        **({"phase": event.phase} if event.phase else {}),
        **({"payload": payload} if payload else {}),
    }


def _metadata_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "call_id",
        "name",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "source",
        "checkpoint_id",
        "granularity",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _redacted_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    sensitive_keys = {
        "text",
        "summary",
        "args",
        "result",
        "error",
        "detail",
        "artifact",
        "data",
        "content",
        "prompt",
        "input",
        "output",
        "message",
        "messages",
        "reasoning",
        "headers",
    }
    safe_string_keys = {
        "status",
        "call_id",
        "name",
        "source",
        "checkpoint_id",
        "granularity",
        "type",
        "phase",
        "role",
        "finish_reason",
    }

    def sensitive(key: Any) -> bool:
        normalized = str(key).strip().lower().replace("-", "_")
        return normalized in sensitive_keys or any(
            marker in normalized
            for marker in ("secret", "password", "authorization", "credential", "api_key")
        ) or normalized in {
            "token",
            "accesstoken",
            "access_token",
            "refreshtoken",
            "refresh_token",
            "authtoken",
            "auth_token",
            "bearer_token",
            "id_token",
            "api_token",
        }

    def redact_item(key: Any, value: Any) -> Any:
        normalized = str(key).strip().lower().replace("-", "_")
        if sensitive(key):
            return "[REDACTED]"
        if isinstance(value, str) and normalized not in safe_string_keys:
            return "[REDACTED]"
        return redact(value)

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact_item(key, item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, tuple):
            return [redact(item) for item in value]
        return value

    return redact(payload)


__all__ = ["EvidenceStore", "EvidenceStoreError", "project_tool_calls"]
