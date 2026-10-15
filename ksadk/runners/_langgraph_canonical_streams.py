"""Stream canonical LangGraph lifecycle, tools, checkpoints, and usage events."""

from __future__ import annotations

import logging
import uuid
from typing import Any, AsyncIterator, Dict

from langgraph.types import Command

from ksadk.events.runtime_event import RuntimeEvent
from ksadk.runners._langgraph_tools_transformer import _CanonicalToolsTransformer
from ksadk.runners.usage_accumulator import accumulate_usage
from ksadk.runtime.skill_eval_result import skill_eval_response_fields

logger = logging.getLogger("ksadk.runners._langgraph_runner_streams")


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
            ContinuationCreated,
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

        # --- parse input (mirrors stream()) ---
        payload = dict(input_data)
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

        # --- build state (same logic as stream()) ---
        if is_checkpoint_resume:
            state = resume_value
        elif is_resume and not is_gateway_approval_resume:
            state = self._unwrap_resume_value(resume_value)
        elif self._has_prepare_state_hook():
            state = self._prepare_state_with_hook(payload, session_id, history)
        else:
            state = self._to_state(payload, history)

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
        if authority is not None and getattr(self, "_managed_a2a_bootstrap", None) is not None:
            authority.require_session(session_id)
            run_id = authority.run_id

        # --- identity ---
        run_scope_id = stable_scope_id("langgraph", run_id, "$run")
        if authority is not None and getattr(self, "_managed_a2a_bootstrap", None) is not None:
            for existing in await authority.list(session_id):
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

        publication_epoch = (
            authority.publication_epoch
            if authority is not None and getattr(self, "_managed_a2a_bootstrap", None) is not None
            else None
        )
        if is_checkpoint_resume and resume_payload_provided:
            publication_epoch = stable_item_id(
                "langgraph",
                publication_epoch or run_id,
                "interaction-resume",
                str(checkpoint_ref.get("checkpoint_id") or ""),
                resume_interrupt_id,
            )
        adapter_context = LangGraphAdapterContext(
            run_id=run_id,
            graph_run_id=native_run_id,
            initial_seq=1,
            checkpoint_ref=adapter_checkpoint_ref,
            publication_epoch=publication_epoch,
        )
        adapter = LangGraphEventAdapter()
        reducer = StreamReducer()
        accumulated_usage: dict[str, Any] = {}
        final_result_fields: dict[str, Any] = {}

        # --- build stream kwargs (v3 rejects stream_mode/subgraphs) ---
        stream_kwargs: dict[str, Any] = {"version": "v3", "config": config}
        if self._callable_accepts_keyword(self._agent.astream_events, "transformers"):
            stream_kwargs["transformers"] = (_CanonicalToolsTransformer,)
        if native_context and self._callable_accepts_keyword(self._agent.astream_events, "context"):
            stream_kwargs["context"] = native_context

        pending_interactions: list[InteractionRequested] = []
        was_interrupted = False
        last_timestamp = started_at

        try:
            run_stream = await self._agent.astream_events(stream_input, **stream_kwargs)
            async for event in adapter.stream_run(run_stream, adapter_context):
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
                if isinstance(event, UsageReported):
                    accumulated_usage = accumulate_usage(
                        accumulated_usage,
                        {
                            "input_tokens": event.input_tokens,
                            "output_tokens": event.output_tokens,
                            "total_tokens": event.total_tokens,
                            "cached_tokens": event.cached_tokens,
                            "reasoning_tokens": event.reasoning_tokens,
                        },
                    )
                    event = event.model_copy(update=accumulated_usage)
                if isinstance(event, RunInterrupted):
                    was_interrupted = True
                    # Extract checkpoint from graph state and emit
                    # ContinuationCreated BEFORE RunInterrupted so downstream
                    # consumers (agui agent) can resolve the resumable
                    # checkpoint_id before processing the terminal interrupt.
                    try:
                        # 统一异步状态读取入口：managed 模式挂 AsyncPostgresSaver，
                        # 在事件循环里同步 get_state 会直接报错。
                        ckpt_state, ckpt_read_error = await self._read_graph_state(config)
                        ckpt_config = getattr(ckpt_state, "config", {}) or {}
                        ckpt_id = str(
                            (ckpt_config.get("configurable") or {}).get("checkpoint_id", "") or ""
                        )
                        if ckpt_id:
                            # Extract next_node from graph state so the
                            # projection layer can surface it in the REST
                            # checkpoint payload.
                            next_nodes_raw = (
                                ckpt_state.get("next")
                                if isinstance(ckpt_state, dict)
                                else getattr(ckpt_state, "next", None)
                            )
                            ckpt_next_node = ""
                            if isinstance(next_nodes_raw, str):
                                ckpt_next_node = next_nodes_raw.strip()
                            elif isinstance(next_nodes_raw, (list, tuple, set)):
                                ckpt_next_node = str(next(iter(next_nodes_raw)) or "").strip()
                            # Enrich source metadata with checkpoint
                            # capability (backend/scope/durable) so the
                            # projection layer does not fall back to unknown.
                            ckpt_capability = self.describe_checkpoint_capability()
                            ckpt_source_metadata: dict[str, Any] = {"checkpoint": True}
                            if isinstance(ckpt_capability, dict):
                                ckpt_is_terminal = not bool(ckpt_next_node)
                                ckpt_is_resumable = bool(
                                    ckpt_capability.get("Supported")
                                    and ckpt_next_node
                                    and ckpt_capability.get("Scope") != "process_local"
                                )
                                ckpt_source_metadata["capability"] = {
                                    "backend": str(ckpt_capability.get("Backend") or "unknown"),
                                    "scope": str(ckpt_capability.get("Scope") or "unknown"),
                                    "durable": bool(ckpt_capability.get("Durable", False)),
                                    "is_terminal": ckpt_is_terminal,
                                    "is_resumable": ckpt_is_resumable,
                                    "resume_status": (
                                        "resumable" if ckpt_is_resumable else "disabled"
                                    ),
                                    "resume_disabled_reason": (
                                        "该 checkpoint 已是终态；可选择更早恢复点重跑"
                                        if ckpt_is_terminal
                                        else str(ckpt_capability.get("Reason") or "")
                                    ),
                                    **({"next_node": ckpt_next_node} if ckpt_next_node else {}),
                                }
                            ckpt_ref = {
                                "thread_id": str(
                                    (ckpt_config.get("configurable") or {}).get(
                                        "thread_id", session_id
                                    )
                                ),
                                # 保留状态自报的真实寻址（子图 ns 带任务路径），
                                # 根 checkpoint 时为空串。恢复按原地址定位。
                                "checkpoint_ns": str(
                                    (ckpt_config.get("configurable") or {}).get("checkpoint_ns", "")
                                    or ""
                                ),
                                "checkpoint_id": ckpt_id,
                                **({"next_node": ckpt_next_node} if ckpt_next_node else {}),
                            }
                            continuation_id = stable_item_id(
                                "langgraph",
                                run_scope_id,
                                "continuation",
                                "graph-checkpoint",
                                ckpt_ref["thread_id"],
                                "checkpoint-ns:",
                                ckpt_id,
                            )
                            cont_event = ContinuationCreated(
                                schema_version=2,
                                event_id=stable_event_id(
                                    "langgraph",
                                    run_scope_id,
                                    continuation_id,
                                    "continuation.created",
                                    "checkpoint",
                                    run_id,
                                    0,
                                ),
                                seq=adapter_context.allocate_placeholder_seq(),
                                timestamp=last_timestamp,
                                run_id=run_id,
                                scope_id=run_scope_id,
                                source=SourceRef(
                                    framework="langgraph",
                                    native_run_id=native_run_id,
                                    metadata=ckpt_source_metadata,
                                ),
                                continuation_id=continuation_id,
                                continuation_kind="graph_checkpoint",
                                resumable=True,
                                ref=ckpt_ref,
                            )
                            reducer.apply(cont_event)
                            yield cont_event
                    except Exception as ckpt_error:
                        # 恢复指针丢失会让这次 interrupt 无法恢复——绝不能静默：
                        # RunInterrupted 仍然发出（下游知道 run 已暂停），但必须
                        # 留下显性告警指向根因。
                        logger.warning(
                            "LangGraph resume checkpoint 读取失败，ContinuationCreated "
                            "未发出（run 可标记为 interrupted 但不可恢复）: %s",
                            ckpt_error,
                        )
                    for interaction in pending_interactions:
                        reducer.apply(interaction)
                        yield interaction
                    pending_interactions.clear()
                    reducer.apply(event)
                    last_timestamp = float(getattr(event, "timestamp", 0.0) or last_timestamp)
                    yield event
                    return
                reducer.apply(event)
                last_timestamp = float(getattr(event, "timestamp", 0.0) or last_timestamp)
                yield event
            # v3 exposes the final graph state after the event stream completes.
            final_state = await run_stream.output()
            final_result_fields = skill_eval_response_fields(final_state)
        except Exception as exc:
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

        # --- emit RunCompleted (skip if RunInterrupted was terminal) ---
        if was_interrupted:
            return

        projection = reducer.snapshot()
        output_refs = tuple(
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
            seq=adapter_context.allocate_placeholder_seq(),
            timestamp=last_timestamp,
            run_id=run_id,
            scope_id=run_scope_id,
            source=terminal_source,
            status="completed",
            output_refs=output_refs,
        )
