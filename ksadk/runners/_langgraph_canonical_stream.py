"""Native canonical LangGraph streaming and persisted interaction publication."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict

from langgraph.errors import is_invalid_resume
from langgraph.stream import ProtocolEvent, StreamTransformer
from langgraph.types import Command

from ksadk.events.runtime_event import RuntimeEvent
from ksadk.runners._langgraph_answer_contract import (
    has_explicit_answer_field,
    internal_model_event,
    uses_explicit_answer_contract,
)
from ksadk.runners._langgraph_fresh_turn import fresh_memory_input, fresh_user_checkpoint
from ksadk.runtime.skill_eval_result import skill_eval_response_fields
from ksadk.runtime.timing import extract_timing

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


class _CanonicalToolsTransformer(StreamTransformer):
    """Subscribe to tools, custom output and actual root state writes.

    The v3 defaults omit ``tools``, ``custom`` and ``updates``. The latter
    distinguishes recovery writes from checkpoint values. A call-site transformer
    requests these without changing the compiled graph or taking ownership of
    the user's ``tool_calls`` projection. Empty projections also coexist
    with configured ToolCallTransformer factories without key collisions.
    Native events pass through unchanged, including nested namespaces;
    only native tool-finished/tool-error events determine tool outcomes.
    """

    required_stream_modes = ("tools", "custom", "updates")

    def init(self) -> dict[str, Any]:
        return {}

    def process(self, event: ProtocolEvent) -> bool:
        return True


class _LangGraphCanonicalStreamMixin:
    async def stream_canonical_events(
        self, input_data: Dict[str, Any]
    ) -> AsyncIterator[RuntimeEvent]:
        from contextlib import aclosing

        from ksadk.a2a.langgraph_runtime import managed_langgraph_events

        async with aclosing(
            managed_langgraph_events(self, self._stream_native_canonical_events(input_data))
        ) as events:
            async for event in events:
                yield event

    @fresh_user_checkpoint
    async def _stream_native_canonical_events(
        self, input_data: Dict[str, Any]
    ) -> AsyncIterator[RuntimeEvent]:
        """Emit canonical RuntimeEvent (schema_version=2) for a LangGraph run.

        Emits RunStarted/RunCompleted/RunFailed lifecycle events and uses
        LangGraphEventAdapter to map item.* events from the v3
        AsyncGraphRunStream.  The old ``stream`` method (dict path) is
        retained for backward compatibility; runner_adapter prefers this
        canonical path when present.
        """

        import time as _time

        from ksadk.events.adapters.langgraph import (
            LangGraphAdapterContext,
            LangGraphEventAdapter,
            LangGraphMappingError,
        )
        from ksadk.events.canonical import (
            ErrorInfo,
            InteractionRequested,
            OutputRef,
            RunCompleted,
            RunFailed,
            RunInterrupted,
            RunProgress,
            RunStarted,
            SourceRef,
            UsageReported,
        )
        from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
        from ksadk.events.reducer import StreamReducer

        await self.prepare_runtime_capabilities()

        # --- parse input (mirrors stream()) ---
        payload = dict(input_data)
        resume_context = payload.pop("_ksadk_native_resume", {})
        observer = None
        run_id = str(
            payload.pop("run_id", None) or payload.pop("invocation_id", None) or ""
        ).strip()
        if not run_id:
            raise ValueError(
                "LangGraph canonical stream requires an explicit run_id or invocation_id"
            )
        payload.pop("_ksadk_force_graph_invoke", None)
        session_id = payload.pop("session_id", None) or str(uuid.uuid4())[:8]
        history = payload.pop("history", [])
        is_resume = payload.pop("resume", False)
        is_checkpoint_resume = bool(payload.pop("checkpoint_resume", False))
        resume_payload_provided = bool(payload.pop("resume_payload_provided", False))
        resume_interrupt_id = str(payload.pop("resume_interrupt_id", "") or "")
        resume_value = payload.get("input")
        is_gateway_approval_resume = bool(
            is_resume and self._is_gateway_approval_semantic_resume(resume_value)
        )
        if is_gateway_approval_resume:
            payload["input"] = self._gateway_approval_follow_up_input()
            resume_value = payload["input"]
        checkpoint_ref = self._extract_langgraph_checkpoint_ref(payload)
        native_context = self.build_native_context(
            payload.get("platform_context"),
            context_schema=getattr(self._agent, "context_schema", None),
        )

        config = await self._get_session_config(session_id)
        if is_checkpoint_resume:
            config = self._apply_checkpoint_resume_config(
                config,
                session_id=session_id,
                checkpoint_ref=checkpoint_ref,
                enforce_bound_thread=bool(self._invocation_identity_scope_ref()),
            )

        if payload.pop("_ksadk_managed_takeover", False):
            from ksadk.events.run_authority import current_run_event_authority

            authority = current_run_event_authority()
            if (
                not is_checkpoint_resume
                or resume_payload_provided
                or getattr(self, "_managed_a2a_bootstrap", None) is None
                or authority is None
            ):
                raise ValueError("A2A_NATIVE_TAKEOVER_CONTEXT_REQUIRED")
            authority.require_session(session_id)
            latest_config = {**config, "configurable": dict(config["configurable"])}
            expected_checkpoint = latest_config["configurable"].pop("checkpoint_id")
            latest, error = await self._read_graph_state(latest_config)
            latest_ref = self._checkpoint_ref_from_state(latest).get("langgraph", {})
            if error or latest_ref.get("checkpoint_id") != expected_checkpoint:
                raise ValueError("A2A_NATIVE_TAKEOVER_CHECKPOINT_DRIFT")
            # Passing an explicit checkpoint ID forks a historical replay in
            # LangGraph. A fenced takeover must continue the validated latest
            # branch so native task IDs and managed request identities survive.
            config = latest_config

        if resume_context.get("submitted"):
            from ksadk.interaction.langgraph_observer import with_resume_observer
            from ksadk.runners._langgraph_interactions import live_resume_config

            config = await live_resume_config(self, config, resume_context)
            config, observer = with_resume_observer(
                config, resume_context["submitted"], resume_context["attempt_key"]
            )

        # --- build state (same logic as stream()) ---
        if is_checkpoint_resume:
            state = resume_value
        elif is_resume and not is_gateway_approval_resume:
            state = self._unwrap_resume_value(resume_value)
        elif self._has_prepare_state_hook():
            state = self._prepare_state_with_hook(payload, session_id, history)
        else:
            state = self._to_state(payload, history)

        if not is_resume and not is_checkpoint_resume:
            state = await fresh_memory_input(self._agent, config, state)

        stream_input = (
            self._checkpoint_resume_input(
                state,
                payload_provided=resume_payload_provided,
                interrupt_id=resume_interrupt_id,
            )
            if is_checkpoint_resume
            else (Command(resume=state) if is_resume and not is_gateway_approval_resume else state)
        )

        # Public lifecycle ownership is separate from the native graph identity.
        # Do this after preparing state so hooks, checkpoints and child/tool IDs
        # continue to receive the original invocation input.
        native_run_id = run_id
        from ksadk.events.run_authority import current_run_event_authority

        authority = current_run_event_authority()
        if authority is not None:
            authority.require_session(session_id)
            run_id = authority.run_id

        # --- identity ---
        run_scope_id = stable_scope_id("langgraph", run_id, "$run")
        prior_events = await authority.list(session_id) if authority is not None else ()
        if authority is not None:
            for existing in prior_events:
                if (
                    isinstance(existing, RunStarted)
                    and existing.run_id == run_id
                    and existing.parent_scope_id is None
                    and existing.source.framework == "langgraph"
                ):
                    run_scope_id = existing.scope_id
                    break
        run_item_id = stable_item_id("langgraph", run_id, "$run")

        source_metadata: dict[str, Any] = {}
        if session_id:
            source_metadata["session_id"] = session_id
        invocation_id = str(input_data.get("invocation_id") or "").strip()
        if invocation_id:
            source_metadata["invocation_id"] = invocation_id
        agent_id = str(input_data.get("agent_id") or "").strip()
        if agent_id:
            source_metadata["agent_id"] = agent_id
        user_id = str(input_data.get("user_id") or "").strip()
        if user_id:
            source_metadata["user_id"] = user_id

        run_source = SourceRef(
            framework="langgraph",
            native_run_id=native_run_id,
            metadata=source_metadata,
        )

        started_at = _time.time()
        yield RunStarted(
            schema_version=2,
            event_id=stable_event_id(
                "langgraph",
                run_scope_id,
                run_item_id,
                "run.started",
                "run",
                run_id,
                0,
            ),
            seq=0,
            timestamp=started_at,
            run_id=run_id,
            scope_id=run_scope_id,
            source=run_source,
            status="running",
        )

        # --- check astream_events availability ---
        if not hasattr(self._agent, "astream_events"):
            terminal_source = SourceRef(
                framework="langgraph",
                native_run_id=native_run_id,
                metadata={**source_metadata, "fallback": "no_astream_events"},
            )
            yield RunCompleted(
                schema_version=2,
                event_id=stable_event_id(
                    "langgraph",
                    run_scope_id,
                    run_item_id,
                    "run.completed",
                    "run",
                    run_id,
                    0,
                ),
                seq=1,
                timestamp=_time.time(),
                run_id=run_id,
                scope_id=run_scope_id,
                source=terminal_source,
                status="completed",
                output_refs=(),
            )
            return

        # --- build adapter context ---
        adapter_checkpoint_ref: dict[str, Any] | None = None
        if checkpoint_ref:
            adapter_checkpoint_ref = dict(checkpoint_ref)
            adapter_checkpoint_ref.setdefault("checkpoint_ns", "")

        publication_epoch = authority.publication_epoch if authority is not None else None
        if is_checkpoint_resume and resume_payload_provided:
            publication_epoch = stable_item_id(
                "langgraph",
                publication_epoch or run_id,
                "interaction-resume",
                str(checkpoint_ref.get("checkpoint_id") or ""),
                resume_interrupt_id or "all",
                str(resume_context.get("attempt_key") or "legacy"),
            )
        adapter_context = LangGraphAdapterContext(
            run_id=run_id,
            graph_run_id=native_run_id,
            initial_seq=1,
            checkpoint_ref=adapter_checkpoint_ref,
            publication_epoch=publication_epoch,
        )
        from ksadk.events.adapters._langgraph_model_tasks import NativeModelTasks
        from ksadk.events.adapters._langgraph_tool_outcomes import with_tool_outcomes
        from ksadk.events.adapters._langgraph_usage_origin import (
            NativeUsageOrigin,
            checkpoint_usage_origins,
            event_usage_origin,
        )

        prior_usage_origins = {
            origin for event in prior_events
            if isinstance(event, UsageReported)
            and event.run_id == run_id
            and (origin := event_usage_origin(event))
        }
        if authority is None and (is_resume or is_checkpoint_resume):
            # Standalone callers have no Kernel event history. Copies of messages
            # already in the input checkpoint aren't new model calls.
            checkpoint, _ = await self._read_graph_state(config, include_subgraphs=True)
            prior_usage_origins = checkpoint_usage_origins(checkpoint)

        config, tool_outcomes = with_tool_outcomes(config)
        model_tasks = NativeModelTasks()
        usage_origin = NativeUsageOrigin()
        config["callbacks"].add_handler(model_tasks, inherit=True)
        config["callbacks"].add_handler(usage_origin, inherit=True)
        adapter = LangGraphEventAdapter(
            tool_outcomes=tool_outcomes,
            model_tasks=model_tasks,
            usage_origin=usage_origin,
            prior_usage_origins=prior_usage_origins,
        )
        reducer = StreamReducer()
        from ksadk.runners._langgraph_final_answer import answer_update_fields

        updated_answer_fields: set[str] = set()
        explicit_answer = uses_explicit_answer_contract(self._agent)
        explicit_snapshot_answer = has_explicit_answer_field(self._agent)
        final_result_fields: dict[str, Any] = {}

        # --- build stream kwargs (v3 rejects stream_mode/subgraphs) ---
        stream_kwargs: dict[str, Any] = {"version": "v3", "config": config}
        if self._callable_accepts_keyword(self._agent.astream_events, "transformers"):
            stream_kwargs["transformers"] = (_CanonicalToolsTransformer,)
        if native_context and self._callable_accepts_keyword(self._agent.astream_events, "context"):
            stream_kwargs["context"] = native_context

        pending_interactions: list[InteractionRequested] = []
        was_interrupted = False
        interrupted_event = None
        final_state = None
        last_timestamp = started_at

        try:
            run_stream = await self._agent.astream_events(stream_input, **stream_kwargs)
            async for event in adapter.stream_run(run_stream, adapter_context):
                updated_answer_fields.update(answer_update_fields(event))
                if (explicit_answer and event.source.metadata.get("channel") != "custom") or (
                    explicit_snapshot_answer
                    and event.source.metadata.get("message_delivery") == "snapshot"
                ):
                    # Whole messages are state snapshots, not emitted tokens.
                    # Mixed messages/output graphs historically chose the final
                    # output when no model/custom text was streamed. Keep their
                    # snapshots observable and let final_answer_events select it.
                    event = internal_model_event(event)
                if isinstance(event, RunInterrupted) or (
                    isinstance(event, RunProgress)
                    and authority is not None
                    and getattr(self, "_managed_a2a_bootstrap", None) is not None
                ):
                    # A native namespace is diagnostic source information, not
                    # a second owner of the public run lifecycle.
                    event = event.model_copy(
                        update={
                            "scope_id": run_scope_id,
                            "parent_scope_id": None,
                            "event_id": stable_event_id(
                                "langgraph",
                                run_scope_id,
                                run_item_id,
                                event.event_type,
                                event.event_id,
                                run_id,
                                0,
                            ),
                        }
                    )
                if isinstance(event, InteractionRequested):
                    # Native resume must be addressable before Kernel records
                    # the interaction. Its checkpoint is read at interruption.
                    pending_interactions.append(event)
                    continue
                if isinstance(event, RunInterrupted):
                    was_interrupted = True
                    interrupted_event = event
                    # v3's interrupt notification precedes final parent writes.
                    # Drain the original stream; do not return or invoke again.
                    continue
                if getattr(event, "item_kind", None) == "data" and event.source.metadata.get(
                    "channel"
                ) in {"values", "updates"}:
                    # Keep native snapshots for history/recovery and gateway
                    # result ownership. They are never assistant answers.
                    event = event.model_copy(
                        update={
                            "source": event.source.model_copy(
                                update={
                                    "metadata": {**event.source.metadata, "visibility": "internal"},
                                }
                            )
                        }
                    )
                reducer.apply(event)
                last_timestamp = float(getattr(event, "timestamp", 0.0) or last_timestamp)
                yield event
            # v3 exposes the final graph state after the event stream completes.
            final_state = await run_stream.output()
            final_result_fields = skill_eval_response_fields(final_state)
            if timing := extract_timing(final_state):
                final_result_fields["timing"] = timing
        except Exception as exc:
            from ksadk.runners._langgraph_interactions import (
                persisted_interactions,
                submission_progress,
            )

            waiting = []
            if observer is not None and is_invalid_resume(exc):
                resume_context = {
                    **resume_context,
                    "consumed": [
                        outcome.interaction_id
                        for outcome in observer.outcomes()
                        if outcome.status == "consumed"
                    ],
                }
                # Only a native validator rejection makes this a correctable
                # wait. A later business error can coexist with other pending
                # branches; their waits must not hide that real failure.
                # Read the persisted root before confirming a rejected branch.
                waiting = await persisted_interactions(
                    self, config, adapter_context, run_scope_id, run_source, resume_context
                )
            progress = submission_progress(adapter_context, run_scope_id, run_source, observer)
            if progress is not None:
                yield progress
                if (
                    any(outcome.status == "rejected" for outcome in progress._outcomes)
                    and all(
                        outcome.status in {"consumed", "rejected"} for outcome in progress._outcomes
                    )
                    and waiting
                ):
                    for event in waiting:
                        yield event
                    yield RunInterrupted(
                        schema_version=2,
                        event_id=stable_event_id(
                            "langgraph",
                            run_scope_id,
                            run_item_id,
                            "run.interrupted",
                            "validation",
                            observer.attempt_key,
                            0,
                        ),
                        seq=adapter_context.allocate_placeholder_seq(),
                        timestamp=_time.time(),
                        run_id=run_id,
                        scope_id=run_scope_id,
                        source=run_source,
                        status="interrupted",
                        reason="input_response_invalid",
                        interaction_id=waiting[-1].interaction_id,
                    )
                    return
            from ksadk.common.diagnostics import safe_exception_locations

            logger.error("LangGraph execution failed; locations=%s", safe_exception_locations(exc))
            error_source = SourceRef(
                framework="langgraph",
                native_run_id=native_run_id,
                metadata={"error_type": type(exc).__name__},
            )
            error_code = exc.code if isinstance(exc, LangGraphMappingError) else "langgraph_failed"
            yield RunFailed(
                schema_version=2,
                event_id=stable_event_id(
                    "langgraph",
                    run_scope_id,
                    run_item_id,
                    "run.failed",
                    "run",
                    run_id,
                    0,
                ),
                seq=adapter_context.allocate_placeholder_seq(),
                timestamp=_time.time(),
                run_id=run_id,
                scope_id=run_scope_id,
                source=error_source,
                status="failed",
                error=ErrorInfo(
                    code=error_code,
                    message=str(exc) or type(exc).__name__,
                    source="langgraph",
                    scope_id=run_scope_id,
                ),
            )
            return

        from ksadk.runners._langgraph_interactions import (
            persisted_interactions,
            submission_progress,
        )

        progress = submission_progress(adapter_context, run_scope_id, run_source, observer)
        if progress is not None:
            resume_context = {
                **resume_context,
                "consumed": [
                    outcome.interaction_id
                    for outcome in progress._outcomes
                    if outcome.status == "consumed"
                ],
            }
            yield progress
        if was_interrupted:
            events = await persisted_interactions(
                self, config, adapter_context, run_scope_id, run_source, resume_context
            )
            if not events:
                raise RuntimeError("Native interrupt has no persisted waiting task")
            for event in events:
                reducer.apply(event)
                yield event
            if interrupted_event is not None:
                interrupted_event = interrupted_event.model_copy(
                    update={
                        "seq": adapter_context.allocate_placeholder_seq(),
                        "timestamp": _time.time(),
                        "interaction_id": events[-1].interaction_id,
                    }
                )
                yield interrupted_event
            return

        # Graphs may return an explicit business answer without emitting model
        # messages. Promote only that answer, never arbitrary graph state.
        from ksadk.runners._langgraph_final_answer import final_answer_events

        answer_events = final_answer_events(
            final_state, reducer.snapshot(), adapter_context, run_scope_id, run_source,
            updated_fields=updated_answer_fields,
            needs_recovery=model_tasks.needs_recovery_answer(),
        )
        for event in answer_events:
            reducer.apply(event)
            yield event

        synthetic_refs = tuple(
            OutputRef(scope_id=event.scope_id, item_id=event.item_id)
            for event in answer_events
            if event.event_type == "item.completed"
        )
        projection = reducer.snapshot()
        output_refs = synthetic_refs or tuple(
            OutputRef(scope_id=item.scope_id, item_id=item.item_id)
            for item in projection.items
            if item.status == "completed"
            and item.item_kind == "message"
            and item.phase == "final_answer"
        )

        terminal_source = SourceRef(
            framework="langgraph",
            native_run_id=native_run_id,
            metadata={
                **source_metadata,
                **({"metrics": final_result_fields} if final_result_fields else {}),
            },
        )
        completed = RunCompleted(
            schema_version=2,
            event_id=stable_event_id(
                "langgraph",
                run_scope_id,
                run_item_id,
                "run.completed",
                "run",
                run_id,
                0,
            ),
            seq=adapter_context.allocate_placeholder_seq(),
            timestamp=last_timestamp,
            run_id=run_id,
            scope_id=run_scope_id,
            source=terminal_source,
            status="completed",
            output_refs=output_refs,
        )
        if getattr(self._agent, "checkpointer", None):
            # Pin this run's final root before publishing completion. A later
            # checkpoint() must not accidentally select another turn's latest.
            import asyncio

            from ksadk.events.native_checkpoint import NativeCheckpointCompleted
            from ksadk.sessions.db_budget import session_db_budget

            completed = NativeCheckpointCompleted(**completed.model_dump())
            expected = config.get("configurable") or {}
            latest = {**config, "configurable": dict(expected)}
            latest["configurable"].pop("checkpoint_id", None)
            try:
                async with asyncio.timeout(session_db_budget().checkpoint_read_operation_seconds):
                    snapshot, error = await self._read_graph_state(latest)
                root = (getattr(snapshot, "config", {}) or {}).get("configurable") or {}
                if (error or not root.get("checkpoint_id") or getattr(snapshot, "next", ())
                        or any(str(root.get(key) or "") != str(expected.get(key) or "")
                               for key in ("thread_id", "checkpoint_ns"))):
                    completed._checkpoint_error = True
                else:
                    completed._checkpoint_ref = {key: str(root.get(key) or "") for key in (
                        "thread_id", "checkpoint_ns", "checkpoint_id",
                    )}
            except Exception:
                # The graph already succeeded. Keep that fact, but never pass
                # an earlier waiting checkpoint off as its completed snapshot.
                completed._checkpoint_error = True
        yield completed
