"""Repair a derived Native trace from matching, already published terminal facts.

This never attaches a runtime or changes the Run, its history, or unknown times.
"""

from __future__ import annotations

import logging

from ksadk.events.canonical import RunCompleted, parse_runtime_event
from ksadk.runtime import RunHandle
from ksadk.studio.contracts import RunStatus
from ksadk.studio.errors import StudioError

_TERMINALS = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}


def _trace_is_complete(service, record) -> bool:
    """Avoid resolving Builds or replaying old histories on every startup."""
    try:
        trace = service.event_store.trace(record.trace_id)
    except StudioError:
        return False
    identity_matches = all(trace.get(key) == value for key, value in {
        "runId": record.id, "agentId": record.agent_id,
        "sessionId": record.session_id, "runtimeType": record.runtime_type,
        "status": RunStatus.COMPLETED.value,
    }.items())
    metrics = trace.get("metrics", {})
    expected = {"durationMs": record.duration_ms, "durationSource": record.duration_source}
    if record.usage.reported:
        expected.update(
            inputTokens=record.usage.input_tokens, outputTokens=record.usage.output_tokens,
            totalTokens=record.usage.total_tokens,
            cachedInputTokens=record.usage.cached_input_tokens,
            reasoningOutputTokens=record.usage.reasoning_output_tokens, usageReported=True,
        )
        if record.usage.source:
            expected["usageSource"] = record.usage.source
    return identity_matches and all(metrics.get(key) == value for key, value in expected.items())


async def rebuild_native_terminal_trace(service, record, resolve_spec) -> bool:
    """Rebuild only a missing/stale projection; unavailable identity stays untouched."""
    native_ref = record.runtime_handle.get("native_ref", {})
    if (record.status != RunStatus.COMPLETED or resolve_spec is None
            or not isinstance(native_ref, dict) or native_ref.get("engine_kind") != "native"):
        return False
    try:
        handle = RunHandle.model_validate(record.runtime_handle)
        if (handle.session_id != record.session_id or handle.runtime_type != record.runtime_type
                or not record.output.strip()):
            return False
        if _trace_is_complete(service, record):
            return False
        options = record.native_runtime_options
        spec = resolve_spec(
            record.build_id, model=record.model or None,
            **({"approval_mode": options.tool_approval_mode} if options is not None else {}),
        )
        if (spec.build_id != record.build_id or spec.agent_id != record.agent_id
                or spec.manifest_sha256 != record.manifest_sha256
                or spec.launch_context.runtime_type != record.runtime_type):
            return False
        events = service.event_store.events(record.id)
        terminals = [event for event in events if event.type in _TERMINALS]
        if len(terminals) != 1 or terminals[0].type != "run.completed":
            return False
        public = terminals[0]
        event = parse_runtime_event(public.data.get("runtimeEvent", {}))
        if (not isinstance(event, RunCompleted) or event.run_id != handle.run_id
                or public.run_id != record.id or public.data.get("runId") != record.id
                or public.data.get("runtimeRunId") != handle.run_id
                or public.data.get("scopeId") != event.scope_id
                or event.run_seq is None or event.run_seq < 1
                or event.source.framework != "ksadk"
                or event.source.native_run_id != handle.run_id
                or not event.source.native_event_id
                or not event.source.native_cursor
                or event.source.metadata.get("engine_kind") != "native"
                or event.source.metadata.get("native_event_kind") != "run.completed"
                or event.source.metadata.get("outcome") != record.verification_outcome):
            return False
        canonical = await service.runtime_events.event_by_id(record.session_id, event.event_id)
        if canonical != event:
            return False
        # All facts already exist. In particular, public.created_at is publication
        # time, not proof of an old execution's completion time or elapsed budget.
        service.event_store.trace_store.sync(record, events)
        return True
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Native terminal trace %s was not rebuilt (%s)", record.id, type(error).__name__
        )
        return False
