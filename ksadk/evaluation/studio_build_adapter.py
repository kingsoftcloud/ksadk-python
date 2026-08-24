"""Evaluation adapter for immutable Studio Build artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ksadk.events.canonical import RuntimeEvent, parse_runtime_event

from .adapters import TargetAdapterError
from .contracts import (
    EvalCase,
    EvalRunSpec,
    TargetKind,
    TargetRef,
    TargetRun,
    TargetRunStatus,
    TargetSnapshot,
    ToolCallEvidence,
    TraceRef,
    UsageSnapshot,
)
from .evidence import EvidenceStore, project_tool_calls


class _StudioRunService(Protocol):
    event_store: Any

    async def run(
        self,
        spec: Any,
        user_input: str,
        *,
        session_id: str,
        on_event: Any = None,
    ) -> Any: ...

    async def events(self, run_id: str, *, after: int = 0) -> list[Any]: ...


@dataclass(frozen=True)
class StudioBuildResolution:
    """Frozen Studio-owned build identity and executable run specification."""

    build_id: str
    agent_id: str
    revision_digest: str
    runtime: str
    model: str | None
    run_spec: Any
    metadata: dict[str, Any]


class StudioBuildTargetError(TargetAdapterError):
    """Classified failure resolving or executing a Studio Build."""


class StudioBuildTargetAdapter:
    """Execute EvalCases through Studio's immutable Build runtime path."""

    kind = TargetKind.STUDIO_BUILD

    def __init__(
        self,
        *,
        timeout_seconds: int,
        resolve_build: Callable[[str], StudioBuildResolution],
        run_service: _StudioRunService,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._resolve_build = resolve_build
        self._run_service = run_service
        self._evidence_store = evidence_store
        self._resolved: StudioBuildResolution | None = None
        self._snapshot: TargetSnapshot | None = None

    def _resolve(self, build_id: str) -> StudioBuildResolution:
        try:
            resolved = self._resolve_build(build_id)
        except StudioBuildTargetError:
            raise
        except Exception as exc:
            raise StudioBuildTargetError(
                "STUDIO_BUILD_RESOLUTION_FAILED",
                "Studio Build could not be resolved",
            ) from exc
        if resolved.build_id != build_id:
            raise StudioBuildTargetError(
                "STUDIO_BUILD_MISMATCH",
                "Studio Build resolver returned a different immutable Build",
            )
        if not resolved.revision_digest or not resolved.runtime:
            raise StudioBuildTargetError(
                "STUDIO_BUILD_INVALID",
                "Studio Build is missing immutable runtime identity",
            )
        return resolved

    async def snapshot(self, target: TargetRef) -> TargetSnapshot:
        if target.kind is not self.kind:
            raise StudioBuildTargetError(
                "STUDIO_BUILD_KIND_INVALID",
                "Studio Build adapter received a non-Build target",
            )
        resolved = self._resolve(target.locator)
        metadata = {
            "buildId": resolved.build_id,
            "agentId": resolved.agent_id,
            **dict(resolved.metadata),
        }
        if resolved.model:
            metadata["model"] = resolved.model
        snapshot = TargetSnapshot(
            kind=self.kind,
            entrypoint=f"build:{resolved.build_id}",
            revision_digest=resolved.revision_digest,
            runtime=resolved.runtime,
            metadata=metadata,
        )
        self._resolved = resolved
        self._snapshot = snapshot
        return snapshot

    async def run_case(
        self,
        spec: EvalRunSpec,
        case: EvalCase,
        *,
        attempt: int,
    ) -> TargetRun:
        resolved = self._resolved
        snapshot = self._snapshot
        if resolved is None or snapshot is None:
            raise RuntimeError("Studio Build target must be snapshotted before execution")
        if spec.target != snapshot:
            raise RuntimeError("EvalRunSpec target does not match the Studio Build snapshot")

        session_id = _scoped_id("eval-build-session", spec.id, case.id, str(attempt))
        output = ""
        duration_ms = 0
        usage = UsageSnapshot()
        trace_ref: TraceRef | None = None
        trace_refs: list[TraceRef] = []
        tool_calls: list[ToolCallEvidence] = []
        for turn in case.turns:
            record = await self._run_service.run(
                resolved.run_spec,
                turn.input,
                session_id=session_id,
            )
            events = _runtime_events(await self._run_service.events(record.id))
            if events:
                tool_calls.extend(project_tool_calls(events))
            trace_ref = _trace_ref(spec.id, record, events)
            if self._evidence_store is not None and events:
                persisted_ref = self._evidence_store.write_trace(
                    spec.id,
                    events,
                    session_id=session_id,
                    policy=spec.config.data_policy,
                )
                trace_ref = persisted_ref.model_copy(
                    update={"trace_id": str(getattr(record, "trace_id", "") or "") or None}
                )
            trace_refs.append(trace_ref)
            duration_ms += max(0, int(getattr(record, "duration_ms", 0) or 0))
            usage = _add_usage(usage, getattr(record, "usage", None))
            status = _status_value(getattr(record, "status", ""))
            if status != "COMPLETED":
                error = getattr(record, "error", None) or {}
                return TargetRun(
                    status=(
                        TargetRunStatus.CANCELLED
                        if status in {"CANCELLED", "INTERRUPTED"}
                        else TargetRunStatus.ERROR
                    ),
                    duration_ms=duration_ms,
                    usage=usage,
                    error_code=str(error.get("code") or "STUDIO_BUILD_RUN_FAILED"),
                    error_message="Studio Build runtime failed",
                    trace_ref=trace_ref,
                    trace_refs=trace_refs,
                    tool_calls=tool_calls,
                    metadata={"runtime": resolved.runtime, "turnCount": len(tool_calls)},
                )
            output = str(getattr(record, "output", "") or "")

        return TargetRun(
            status=TargetRunStatus.PASSED if output else TargetRunStatus.UNAVAILABLE,
            output=output,
            duration_ms=duration_ms,
            usage=usage,
            error_code=None if output else "STUDIO_BUILD_OUTPUT_UNAVAILABLE",
            error_message=None if output else "Studio Build did not provide evaluable text output",
            trace_ref=trace_ref,
            trace_refs=trace_refs,
            tool_calls=tool_calls,
            metadata={"runtime": resolved.runtime, "turnCount": len(case.turns)},
        )


def _runtime_events(stored_events: list[Any]) -> list[RuntimeEvent]:
    events: list[RuntimeEvent] = []
    for stored in stored_events:
        data = getattr(stored, "data", None)
        payload = data.get("runtimeEvent") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            continue
        try:
            events.append(parse_runtime_event(payload))
        except ValueError:
            continue
    return sorted(events, key=lambda event: event.seq)


def _trace_ref(run_id: str, record: Any, events: list[RuntimeEvent]) -> TraceRef:
    return TraceRef(
        run_id=run_id,
        trace_id=str(getattr(record, "trace_id", "") or "") or None,
        session_id=str(getattr(record, "session_id", "") or "") or None,
        invocation_id=str(getattr(record, "id", "") or "") or None,
        seq_start=events[0].seq if events else None,
        seq_end=events[-1].seq if events else None,
    )


def _add_usage(total: UsageSnapshot, raw: Any) -> UsageSnapshot:
    return UsageSnapshot(
        input_tokens=total.input_tokens + max(0, int(getattr(raw, "input_tokens", 0) or 0)),
        output_tokens=total.output_tokens + max(0, int(getattr(raw, "output_tokens", 0) or 0)),
        total_tokens=total.total_tokens + max(0, int(getattr(raw, "total_tokens", 0) or 0)),
        reported=total.reported or bool(getattr(raw, "reported", False)),
    )


def _status_value(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    return str(value or "").upper()


def _scoped_id(prefix: str, *parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


__all__ = [
    "StudioBuildResolution",
    "StudioBuildTargetAdapter",
    "StudioBuildTargetError",
]
