"""Policy-neutral RuntimeEvent projections used by evaluation adapters."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ksadk.events.canonical import (
    ContextCompactionCompleted,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RuntimeEvent,
    dump_runtime_event,
)
from ksadk.events.content import TextContent, ToolCallContent, ToolResultContent

from .contracts import (
    AssertionType,
    DataPolicy,
    EvalCase,
    FileCheckEvidence,
    ToolCallEvidence,
    TraceRef,
)

_SAFE_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_MAX_VERIFICATION_FILE_BYTES = 8 * 1024 * 1024


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
        session_id: str,
        policy: DataPolicy = DataPolicy.LOCAL_ONLY,
    ) -> TraceRef:
        event_list = sorted(events, key=lambda item: item.seq)
        if not event_list:
            raise EvidenceStoreError("RuntimeEvent evidence must not be empty")
        if not session_id.strip():
            raise EvidenceStoreError("RuntimeEvent evidence requires an explicit session_id")
        invocation_ids = {event.run_id for event in event_list}
        if len(invocation_ids) != 1:
            raise EvidenceStoreError("RuntimeEvent evidence must describe one invocation")
        invocation_id = next(iter(invocation_ids))
        path = self._trace_path(run_id, session_id, invocation_id)
        payload = {
            "schemaVersion": "ksadk.eval.evidence/v2",
            "runId": run_id,
            "sessionId": session_id,
            "invocationId": invocation_id,
            "dataPolicy": policy.value,
            "seqStart": event_list[0].seq,
            "seqEnd": event_list[-1].seq,
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
            seq_start=event_list[0].seq,
            seq_end=event_list[-1].seq,
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
        identity = "\0".join((run_id, session_id, invocation_id)).encode("utf-8")
        return self.root / "evidence" / f"{hashlib.sha256(identity).hexdigest()}.json"


def project_tool_calls(events: Iterable[RuntimeEvent]) -> list[ToolCallEvidence]:
    """Project tool lifecycle events without retaining arguments or results.

    Framework adapters normally emit canonical ``tool_call`` items.  Managed
    Harness also publishes its stable public lifecycle as structured status
    facts (``tool.call.begin`` / ``tool.call.end``).  Accept both projections
    so evaluation measures the same execution fact instead of the adapter's
    presentation shape.
    """

    calls: dict[str, ToolCallEvidence] = {}
    order: list[str] = []
    for event in sorted(events, key=lambda item: item.seq):
        status_payload = _status_payload(event)
        status_event = str(status_payload.get("event") or "")
        status_details = status_payload.get("details")
        if status_event in {"tool.call.begin", "tool.call.end"} and isinstance(
            status_details, dict
        ):
            call_id = str(
                status_details.get("call_id") or status_details.get("callId") or ""
            ).strip()
            name = str(status_details.get("name") or "unknown").strip() or "unknown"
            if call_id:
                current = calls.get(call_id)
                if current is None:
                    order.append(call_id)
                    current = ToolCallEvidence(
                        call_id=call_id,
                        name=name,
                        status="INCOMPLETE",
                        seq_start=event.seq,
                    )
                if status_event == "tool.call.end":
                    terminal = str(status_details.get("status") or "").lower()
                    current = current.model_copy(
                        update={
                            "name": name if current.name == "unknown" else current.name,
                            "status": (
                                "SUCCEEDED"
                                if terminal in {"completed", "succeeded", "success"}
                                else "ERROR"
                            ),
                            "seq_end": event.seq,
                        }
                    )
                calls[call_id] = current
        parts = ()
        if isinstance(event, ItemStarted) and event.item_kind == "tool_call" and event.initial:
            parts = event.initial.parts
        elif isinstance(event, ItemCompleted) and event.item_kind == "tool_call":
            parts = event.snapshot.parts
        completed_call_ids: list[str] = []
        for part in parts:
            if isinstance(part, ToolCallContent):
                current = calls.get(part.call_id)
                if current is None:
                    order.append(part.call_id)
                    current = ToolCallEvidence(
                        call_id=part.call_id,
                        name=part.name,
                        status="INCOMPLETE",
                        seq_start=event.seq,
                    )
                calls[part.call_id] = current
                if isinstance(event, ItemCompleted):
                    completed_call_ids.append(part.call_id)
            elif isinstance(part, ToolResultContent):
                current = calls.get(part.call_id)
                if current is None:
                    # A provider may emit a terminal snapshot after reconnecting;
                    # retain it as evidence instead of discarding the fact.
                    order.append(part.call_id)
                    current = ToolCallEvidence(
                        call_id=part.call_id,
                        name="unknown",
                        status="INCOMPLETE",
                    )
                calls[part.call_id] = current.model_copy(
                    update={
                        "status": "ERROR" if part.is_error else "SUCCEEDED",
                        "seq_end": event.seq,
                    }
                )
        # Some canonical framework projectors close the tool item without
        # repeating its result payload.  The completed lifecycle is still a
        # terminal success fact; an explicit ToolResultContent error above
        # remains authoritative and is never overwritten here.
        for call_id in completed_call_ids:
            current = calls[call_id]
            if current.status == "INCOMPLETE":
                calls[call_id] = current.model_copy(
                    update={"status": "SUCCEEDED", "seq_end": event.seq}
                )
    return [calls[call_id] for call_id in order]


def project_runtime_facts(events: Iterable[RuntimeEvent]) -> dict[str, int]:
    """Derive non-sensitive, deterministic counters from canonical events.

    Counts are deliberately based on event identity and lifecycle metadata,
    never model reasoning, tool arguments or tool results.  This makes them
    suitable for same-task Harness comparisons without leaking trace content.
    """

    terminal_by_scope: dict[tuple[str, str], set[tuple[str, str]]] = {}
    retry_ids: set[tuple[str, str]] = set()
    compaction_ids: set[tuple[str, str]] = set()
    child_scopes: set[tuple[str, str]] = set()
    subagent_call_ids: set[tuple[str, str]] = set()
    memory_recall_ids: set[tuple[str, str]] = set()
    memory_write_ids: set[tuple[str, str]] = set()
    skill_disclosure_ids: set[tuple[str, str]] = set()
    mcp_disclosure_ids: set[tuple[str, str]] = set()

    for event in sorted(events, key=lambda item: item.seq):
        source = event.source
        native_run_id = str(source.native_run_id or event.run_id)
        native_event_id = str(source.native_event_id or event.event_id)
        identity = (native_run_id, native_event_id)

        if isinstance(event, (RunCompleted, RunFailed, RunCanceled)):
            terminal_by_scope.setdefault((event.run_id, event.scope_id), set()).add(identity)
        native_type = str(source.metadata.get("native_event_type") or "")
        if (
            isinstance(event, ContextCompactionCompleted)
            or native_type == "context.compaction.completed"
        ):
            compaction_ids.add(identity)
        # Some provider-native status facts use the current scope as their
        # parent for grouping.  That is not a child Agent and must not satisfy
        # a subagent assertion.
        if event.parent_scope_id and event.parent_scope_id != event.scope_id:
            child_scopes.add((event.run_id, event.scope_id))

        details = _status_details(event)
        if native_type == "memory.recalled":
            memory_recall_ids.add(identity)
        elif native_type == "memory.write" and str(details.get("decision") or "") == "commit":
            memory_write_ids.add(identity)
        elif native_type == "skill.disclosed":
            skill_disclosure_ids.add(identity)
        elif native_type == "mcp.disclosed":
            mcp_disclosure_ids.add(identity)
        detail_kind = str(source.metadata.get("activity_kind") or details.get("kind") or "")
        failure_action = str(source.metadata.get("failure_action") or "")
        if (
            native_type == "model.call.failed"
            and (
                failure_action == "retry_same_model"
                or (
                    not failure_action
                    and getattr(event, "item_kind", "") != "status"
                )
            )
        ) or detail_kind == "provider.retry":
            retry_ids.add(identity)
        if detail_kind in {"delegation.route", "subagent.event"}:
            call_id = str(
                source.metadata.get("activity_call_id")
                or details.get("call_id")
                or details.get("callId")
                or ""
            ).strip()
            if call_id:
                subagent_call_ids.add((native_run_id, call_id))

    return {
        "maxTerminalEventsPerScope": max(
            (len(identities) for identities in terminal_by_scope.values()),
            default=0,
        ),
        "retryCount": len(retry_ids),
        "compactionCount": len(compaction_ids),
        "subagentCount": (
            len(subagent_call_ids) if subagent_call_ids else len(child_scopes)
        ),
        "memoryRecallCount": len(memory_recall_ids),
        "memoryWriteCount": len(memory_write_ids),
        "skillDisclosureCount": len(skill_disclosure_ids),
        "mcpDisclosureCount": len(mcp_disclosure_ids),
    }


def project_file_checks(case: EvalCase, root: Path | None) -> list[FileCheckEvidence]:
    """Check declared relative files without retaining their generated content."""

    checks: list[FileCheckEvidence] = []
    file_assertion_types = {
        AssertionType.ARTIFACT_FILE_EXISTS,
        AssertionType.ARTIFACT_FILE_EQUALS,
        AssertionType.ARTIFACT_FILE_CONTAINS,
    }
    for assertion in case.assertions:
        if assertion.type not in file_assertion_types:
            continue
        if isinstance(assertion.value, str):
            relative_path = assertion.value
            expected = None
        else:
            relative_path = str(assertion.value["path"])
            expected = str(assertion.value["content"])
        expected_digest = (
            f"sha256:{hashlib.sha256(expected.encode('utf-8')).hexdigest()}"
            if expected is not None
            else None
        )
        base = {
            "assertion_type": assertion.type.value,
            "path": relative_path,
            "expected_digest": expected_digest,
        }
        if root is None:
            checks.append(FileCheckEvidence(**base, status="UNAVAILABLE"))
            continue
        try:
            resolved_root = root.resolve(strict=True)
            target = (resolved_root / relative_path).resolve(strict=False)
            if not target.is_relative_to(resolved_root):
                checks.append(FileCheckEvidence(**base, status="ERROR"))
                continue
            if not target.exists():
                checks.append(FileCheckEvidence(**base, status="MISSING"))
                continue
            if not target.is_file():
                checks.append(FileCheckEvidence(**base, status="NOT_FILE"))
                continue
            size = target.stat().st_size
            if assertion.type is AssertionType.ARTIFACT_FILE_EXISTS:
                checks.append(FileCheckEvidence(**base, status="PRESENT", size_bytes=size))
                continue
            if size > _MAX_VERIFICATION_FILE_BYTES:
                checks.append(FileCheckEvidence(**base, status="TOO_LARGE", size_bytes=size))
                continue
            content = target.read_bytes()
            actual_sha256 = f"sha256:{hashlib.sha256(content).hexdigest()}"
            expected_bytes = (expected or "").encode("utf-8")
            matched = (
                content == expected_bytes
                if assertion.type is AssertionType.ARTIFACT_FILE_EQUALS
                else expected_bytes in content
            )
            checks.append(
                FileCheckEvidence(
                    **base,
                    status="MATCH" if matched else "MISMATCH",
                    actual_sha256=actual_sha256,
                    size_bytes=len(content),
                )
            )
        except OSError:
            checks.append(FileCheckEvidence(**base, status="ERROR"))
    return checks


def _status_details(event: RuntimeEvent) -> dict[str, Any]:
    """Read only structured status facts emitted by the canonical projector."""

    payload = _status_payload(event)
    details = payload.get("details")
    return details if isinstance(details, dict) else {}


def _status_payload(event: RuntimeEvent) -> dict[str, Any]:
    """Parse one structured status payload without exposing free-form text."""

    if getattr(event, "item_kind", "") != "status":
        return {}
    parts: tuple[Any, ...] = ()
    if isinstance(event, ItemStarted) and event.initial:
        parts = event.initial.parts
    elif isinstance(event, ItemCompleted):
        parts = event.snapshot.parts
    elif isinstance(event, ItemUpdated):
        parts = (event.update,)
    for part in parts:
        if not isinstance(part, TextContent):
            continue
        try:
            payload = json.loads(part.text)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        event_name = payload.get("event")
        details = payload.get("details")
        if isinstance(event_name, str) and isinstance(details, dict):
            return {"event": event_name, "details": details}
    return {}


def _event_payload(event: RuntimeEvent, policy: DataPolicy) -> dict[str, Any]:
    payload = dict(dump_runtime_event(event))
    if policy is DataPolicy.METADATA_ONLY:
        payload = _metadata_payload(event.event_type, payload)
    elif policy is DataPolicy.REDACTED_TRACE:
        payload = _redacted_payload(event.event_type, payload)
    return {
        "schemaVersion": event.schema_version,
        "eventId": event.event_id,
        "eventType": event.event_type,
        "timestamp": event.timestamp,
        "runId": event.run_id,
        "seq": event.seq,
        "event": payload,
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


__all__ = [
    "EvidenceStore",
    "EvidenceStoreError",
    "project_file_checks",
    "project_runtime_facts",
    "project_tool_calls",
]
