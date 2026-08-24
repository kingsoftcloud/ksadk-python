"""LangGraphRunner 的 stream / stream_canonical_events 实现（纯移动自 langgraph_runner，行为不变）。

以 mixin 形式被 :class:`LangGraphRunner` 继承。
"""

from __future__ import annotations

import inspect
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Mapping

from langgraph.types import Command

from ksadk.conversations.reasoning_markup import ReasoningMarkupParser, strip_reasoning_markup
from ksadk.events.runtime_event import RuntimeEvent
from ksadk.runners.usage_accumulator import accumulate_usage

if TYPE_CHECKING:
    pass


class _LangGraphStreamMixin:
    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LangGraph 图"""
        payload = dict(input_data)
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
            # See ``invoke``: the graph did not suspend at a native interrupt,
            # so use the durable transcript to run the post-tool answer turn.
            payload["input"] = self._gateway_approval_follow_up_input()
            resume_value = payload["input"]
        checkpoint_ref = self._extract_langgraph_checkpoint_ref(payload)
        native_context = self.build_native_context(payload.get("platform_context"))
        invoke_payload = dict(payload)
        invoke_payload["session_id"] = session_id
        if history:
            invoke_payload["history"] = history
        if is_resume and not is_gateway_approval_resume:
            invoke_payload["resume"] = True
        if is_checkpoint_resume:
            invoke_payload["checkpoint_resume"] = True
            invoke_payload["resume_payload_provided"] = resume_payload_provided
            invoke_payload["resume_interrupt_id"] = resume_interrupt_id

        config = self._get_config(session_id)
        if is_checkpoint_resume:
            config = self._apply_checkpoint_resume_config(
                config,
                session_id=session_id,
                checkpoint_ref=checkpoint_ref,
            )

        if is_checkpoint_resume:
            state = resume_value
        elif is_resume and not is_gateway_approval_resume:
            # Keep the interrupt value intact for ``Command(resume=...)``;
            # prepare-state hooks only shape fresh user turns.
            state = resume_value
        elif self._has_prepare_state_hook():
            state = self._prepare_state_with_hook(
                payload,
                session_id,
                history,
                is_resume=is_gateway_approval_resume,
            )
        else:
            state = self._to_state(payload, history)

        accumulated_text = ""
        accumulated_reasoning = ""
        inline_reasoning_parser = ReasoningMarkupParser()
        emitted_non_text_event = False
        final_output_text = ""
        final_output_usage: dict[str, Any] = {}
        final_output_last_usage: dict[str, Any] = {}
        model_run_usages: dict[str, dict[str, Any]] = {}
        model_run_order: list[str] = []
        stream_usage_run_keys: set[str] = set()
        latest_stream_usage: dict[str, Any] = {}
        model_started_at: dict[str, float] = {}
        model_step_indexes: dict[str, int] = {}
        first_token_seen: set[str] = set()
        next_step_index = 0

        def model_run_key(
            event: Mapping[str, Any],
            *,
            fallback_key: str | None = None,
        ) -> str:
            raw_run_id = event.get("run_id")
            return (
                str(raw_run_id)
                if raw_run_id
                else fallback_key or f"model-event-{len(model_run_order)}"
            )

        def record_model_usage(
            event: Mapping[str, Any],
            usage: dict[str, Any],
            *,
            fallback_key: str | None = None,
        ) -> None:
            if not usage:
                return
            run_key = model_run_key(event, fallback_key=fallback_key)
            if run_key not in model_run_usages:
                model_run_order.append(run_key)
            model_run_usages[run_key] = dict(usage)

        def accumulated_model_usage() -> dict[str, Any]:
            if len(model_run_order) == 1:
                return dict(model_run_usages.get(model_run_order[0]) or {})
            usage: dict[str, Any] = {}
            for run_key in model_run_order:
                usage = accumulate_usage(usage, model_run_usages.get(run_key) or {})
            return usage

        def latest_model_usage() -> dict[str, Any]:
            for run_key in reversed(model_run_order):
                usage = model_run_usages.get(run_key)
                if usage:
                    return dict(usage)
            return {}

        if is_checkpoint_resume and callable(getattr(self._agent, "astream", None)):
            try:
                async for chunk in self._stream_checkpoint_resume_updates(
                    stream_input=self._checkpoint_resume_input(
                        state,
                        payload_provided=resume_payload_provided,
                        interrupt_id=resume_interrupt_id,
                    ),
                    config=config,
                    context=native_context,
                ):
                    yield chunk
                return
            except Exception as e:
                yield {
                    "type": "error",
                    "message": str(e) or "LangGraph checkpoint resume failed",
                    "checkpoint_id": str(checkpoint_ref.get("checkpoint_id") or ""),
                    "exception_type": type(e).__name__,
                }
                return

        if not hasattr(self._agent, "astream_events"):
            result = await self.invoke(invoke_payload)
            final_chunk = {"output": result.get("output", ""), "type": "final"}
            usage = self._extract_usage(result)
            if usage:
                final_chunk["usage"] = usage
            last_usage = self._extract_last_usage(result)
            if last_usage:
                final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
            yield final_chunk
            return

        try:
            stream_input = (
                self._checkpoint_resume_input(
                    state,
                    payload_provided=resume_payload_provided,
                    interrupt_id=resume_interrupt_id,
                )
                if is_checkpoint_resume
                else (
                    Command(resume=state) if is_resume and not is_gateway_approval_resume else state
                )
            )
            # stream_mode 含 "custom" 才会产生 on_custom_stream 事件(custom writer);
            # 保留默认 "values" 以兼容既有 on_chain_end/graph_update 消费。
            stream_kwargs = {"version": "v2", "config": config}
            if self._callable_accepts_keyword(self._agent.astream_events, "stream_mode"):
                stream_kwargs["stream_mode"] = ["values", "custom"]
            if native_context and self._callable_accepts_keyword(
                self._agent.astream_events, "context"
            ):
                stream_kwargs["context"] = native_context
            async for event in self._agent.astream_events(stream_input, **stream_kwargs):
                event_kind = event.get("event", "")

                if event_kind == "on_chat_model_start":
                    model_call_id = str(event.get("run_id") or "")
                    if model_call_id:
                        next_step_index += 1
                        step_id = f"step_{model_call_id}"
                        model_started_at[model_call_id] = time.monotonic()
                        model_step_indexes[model_call_id] = next_step_index
                        yield {
                            "type": "step_start",
                            "step_id": step_id,
                            "step_index": next_step_index,
                        }
                        yield {
                            "type": "model_call_begin",
                            "step_id": step_id,
                            "model_call_id": model_call_id,
                            "model": str(event.get("name") or "chat-model"),
                        }

                elif event_kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if not chunk:
                        continue
                    model_call_id = str(event.get("run_id") or "")
                    if (
                        model_call_id in model_started_at
                        and model_call_id not in first_token_seen
                    ):
                        reasoning_content = getattr(chunk, "reasoning_content", None)
                        if not reasoning_content and hasattr(chunk, "additional_kwargs"):
                            reasoning_content = chunk.additional_kwargs.get(
                                "reasoning_content"
                            )
                        if getattr(chunk, "content", None) or reasoning_content:
                            first_token_seen.add(model_call_id)
                            yield {
                                "type": "model_call_first_token",
                                "step_id": f"step_{model_call_id}",
                                "model_call_id": model_call_id,
                                "ttft_ms": int(
                                    (time.monotonic() - model_started_at[model_call_id])
                                    * 1000
                                ),
                            }
                    chunk_usage = self._extract_usage(chunk)
                    if chunk_usage:
                        # Some LangChain providers attach cumulative usage to
                        # every stream chunk, and LangChain may then sum those
                        # cumulative snapshots into an inflated
                        # on_chat_model_end usage. For a concrete model run,
                        # keep the latest stream snapshot and ignore the later
                        # end usage for that same run_id.
                        latest_stream_usage = dict(chunk_usage)
                        if event.get("run_id"):
                            run_key = model_run_key(event)
                            stream_usage_run_keys.add(run_key)
                            record_model_usage(event, latest_stream_usage)

                    # 推理内容
                    reasoning = getattr(chunk, "reasoning_content", None)
                    if not reasoning and hasattr(chunk, "additional_kwargs"):
                        reasoning = chunk.additional_kwargs.get("reasoning_content")

                    if reasoning:
                        accumulated_reasoning += reasoning
                        yield {"delta": reasoning, "type": "thinking"}

                    # 常规内容
                    if hasattr(chunk, "content") and chunk.content:
                        content = self._filter_tool_tags(chunk.content)
                        if isinstance(content, str):
                            if accumulated_reasoning and content.startswith(accumulated_reasoning):
                                content = content[len(accumulated_reasoning) :]
                            elif reasoning and content.startswith(reasoning):
                                content = content[len(reasoning) :]
                        if content:
                            for part in inline_reasoning_parser.feed(content):
                                if not part.text:
                                    continue
                                if part.kind == "thinking":
                                    accumulated_reasoning += part.text
                                    yield {"delta": part.text, "type": "thinking"}
                                else:
                                    accumulated_text += part.text
                                    yield {"delta": part.text, "type": "text"}

                elif event_kind == "on_chat_model_end":
                    data = event.get("data") or {}
                    output = data.get("output") if isinstance(data, Mapping) else None
                    usage = self._extract_usage(output) or self._extract_usage(data)
                    last_usage = self._extract_last_usage(output) or self._extract_last_usage(data)
                    run_key = model_run_key(event)
                    if run_key not in stream_usage_run_keys:
                        record_model_usage(event, last_usage or usage)
                    model_call_id = str(event.get("run_id") or "")
                    started_at = model_started_at.pop(model_call_id, None)
                    step_index = model_step_indexes.pop(model_call_id, None)
                    if started_at is not None and step_index is not None:
                        duration_ms = int((time.monotonic() - started_at) * 1000)
                        step_id = f"step_{model_call_id}"
                        yield {
                            "type": "model_call_end",
                            "step_id": step_id,
                            "model_call_id": model_call_id,
                            "status": "completed",
                            "duration_ms": duration_ms,
                        }
                        yield {
                            "type": "step_end",
                            "step_id": step_id,
                            "step_index": step_index,
                            "status": "completed",
                            "duration_ms": duration_ms,
                        }

                elif event_kind == "on_chain_stream":
                    # node 内 get_stream_writer() 写入的自定义数据,经 stream_mode 含
                    # "custom" 时,astream_events 包成 on_chain_stream,chunk 为
                    # (mode, value) tuple:("custom", value) 是 writer 透传内容,
                    # ("values", state) 是 state 快照(忽略,终态走 on_chain_end)。
                    # 编排方常用 custom writer 把"调远端 agent/子图"的流式增量透传出来。
                    chunk = event.get("data", {}).get("chunk")
                    if not (isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == "custom"):
                        continue
                    data = chunk[1]
                    if isinstance(data, str):
                        accumulated_text += data
                        yield {"delta": data, "type": "text"}
                        continue
                    if isinstance(data, Mapping):
                        custom_type = str(data.get("type") or "text")
                        if custom_type in ("tool_call", "tool_result"):
                            # 结构化工具事件:透传完整 payload(tool_name/tool_args/
                            # tool_output 等),不计入正文,供 UI 渲染工具卡片。
                            out = {"type": custom_type}
                            out.update({k: v for k, v in data.items() if k != "type"})
                            yield out
                            continue
                        custom_delta = ""
                        for key in ("delta", "text", "content", "output", "data"):
                            value = data.get(key)
                            if isinstance(value, str) and value:
                                custom_delta = value
                                break
                        if not custom_delta:
                            continue
                        replace = bool(data.get("replace"))
                        if custom_type == "thinking":
                            accumulated_reasoning = (
                                custom_delta if replace else accumulated_reasoning + custom_delta
                            )
                        else:
                            accumulated_text = (
                                custom_delta if replace else accumulated_text + custom_delta
                            )
                        custom_event: dict[str, Any] = {
                            "delta": custom_delta,
                            "type": custom_type,
                        }
                        if replace:
                            custom_event["replace"] = True
                        yield custom_event
                        continue
                    if data is not None:
                        accumulated_text += str(data)
                        yield {"delta": str(data), "type": "text"}

                elif event_kind == "on_tool_start":
                    emitted_non_text_event = True
                    yield {
                        "type": "tool_call",
                        "tool_name": event.get("name", "unknown"),
                        "tool_args": event.get("data", {}).get("input", {}),
                        "run_id": event.get("run_id"),
                    }

                elif event_kind == "on_tool_end":
                    emitted_non_text_event = True
                    tool_output = event.get("data", {}).get("output", "")
                    # LangGraph returns a ToolMessage here for normal tools.
                    # Preserve its content instead of serializing the repr,
                    # otherwise structured output such as A2UI envelopes becomes
                    # unparsable. Keep the callback run_id below: it is paired
                    # with the preceding ``on_tool_start`` event on this stream.
                    normalized_output = getattr(tool_output, "content", tool_output)
                    if isinstance(tool_output, Mapping) and "content" in tool_output:
                        normalized_output = tool_output["content"]
                    yield {
                        "type": "tool_result",
                        "tool_name": event.get("name", "unknown"),
                        "tool_args": event.get("data", {}).get("input", {}),
                        "tool_output": normalized_output,
                        "run_id": event.get("run_id"),
                    }

                elif event_kind == "on_chain_end":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict) and "__interrupt__" in output:
                        emitted_non_text_event = True
                        yield {
                            "type": "interrupt",
                            "interrupt_info": output["__interrupt__"],
                            "session_id": session_id,
                        }
                        return
                    extracted_output = self._extract_output(output)
                    if extracted_output:
                        final_output_text = strip_reasoning_markup(str(extracted_output))
                    final_output_usage = self._extract_usage(output)
                    final_output_last_usage = self._extract_last_usage(output)

        except Exception as e:
            if "Interrupt" in type(e).__name__:
                yield {
                    "type": "interrupt",
                    "interrupt_info": self._get_interrupt_info(self._agent.get_state(config)),
                    "session_id": session_id,
                }
                return
            raise

        # goal-18(ksadk-web 人机交互):图因审批门(HITL)在流式中静默暂停时,
        # 这里把审批详情(action_requests)作为 approval 事件冒出,供 UI 渲染审批卡。
        # 此前流式路径只在 checkpoint 标 resumable,UI 拿不到"该批哪个工具/什么参数/允许哪些决定"。
        # 注:get_state 在部分 agent 上是 async,统一按 awaitable 处理;取不到则跳过,不破坏事件流。
        pending_approval = None
        try:
            _get_state = getattr(self._agent, "aget_state", None) or getattr(
                self._agent, "get_state", None
            )
            if _get_state is not None:
                _maybe_state = _get_state(config)
                if inspect.isawaitable(_maybe_state):
                    _maybe_state = await _maybe_state
                pending_approval = self._get_interrupt_info(_maybe_state)
        except Exception:
            pending_approval = None
        if pending_approval:
            yield {
                "type": "approval",
                "interrupt_info": pending_approval,
                "session_id": session_id,
            }
            metadata = await self._latest_checkpoint_metadata(config)
            if metadata:
                yield {"type": "checkpoint", "metadata": metadata}
            return

        for part in inline_reasoning_parser.flush():
            if not part.text:
                continue
            if part.kind == "thinking":
                accumulated_reasoning += part.text
                yield {"delta": part.text, "type": "thinking"}
            else:
                accumulated_text += part.text
                yield {"delta": part.text, "type": "text"}

        if not accumulated_text:
            if final_output_text:
                final_chunk = {"output": final_output_text, "type": "final"}
                usage = accumulated_model_usage() or final_output_usage or latest_stream_usage
                last_usage = (
                    latest_model_usage() or final_output_last_usage or latest_stream_usage or usage
                )
                if usage:
                    final_chunk["usage"] = usage
                if last_usage:
                    final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
                yield final_chunk
            elif not emitted_non_text_event:
                result = await self.invoke({**invoke_payload, "_ksadk_force_graph_invoke": True})
                fallback_chunk: dict[str, Any] = {
                    "output": result.get("output", ""),
                    "type": "final",
                }
                usage = self._extract_usage(result)
                if usage:
                    fallback_chunk["usage"] = usage
                last_usage = self._extract_last_usage(result)
                if last_usage:
                    fallback_chunk.setdefault("metadata", {})["last_usage"] = last_usage
                yield fallback_chunk
                checkpoint_metadata = result.get("metadata") if isinstance(result, dict) else None
                if isinstance(checkpoint_metadata, dict) and checkpoint_metadata.get("agentengine"):
                    yield {"type": "checkpoint", "metadata": checkpoint_metadata}
                    return
        else:
            final_chunk = {"output": accumulated_text, "type": "final"}
            state_usage = await self._latest_state_usage(config)
            usage = (
                accumulated_model_usage()
                or state_usage
                or final_output_usage
                or latest_stream_usage
            )
            if usage:
                final_chunk["usage"] = usage
                last_usage = (
                    latest_model_usage()
                    or state_usage
                    or final_output_last_usage
                    or latest_stream_usage
                    or usage
                )
                final_chunk.setdefault("metadata", {})["last_usage"] = last_usage
            yield final_chunk

        metadata = await self._latest_checkpoint_metadata(config)
        if metadata:
            yield {"type": "checkpoint", "metadata": metadata}

    async def stream_canonical_events(
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
            OutputRef,
            RunCompleted,
            RunFailed,
            RunInterrupted,
            RunStarted,
            SourceRef,
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
        checkpoint_ref = self._extract_langgraph_checkpoint_ref(payload)
        native_context = self.build_native_context(payload.get("platform_context"))

        config = self._get_config(session_id)
        if is_checkpoint_resume:
            config = self._apply_checkpoint_resume_config(
                config,
                session_id=session_id,
                checkpoint_ref=checkpoint_ref,
            )

        # --- build state (same logic as stream()) ---
        if is_checkpoint_resume:
            state = resume_value
        elif is_resume:
            state = resume_value
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
            else (Command(resume=state) if is_resume else state)
        )

        # --- identity ---
        run_scope_id = stable_scope_id("langgraph", run_id, "$run")
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
            native_run_id=run_id,
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
                native_run_id=run_id,
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

        adapter_context = LangGraphAdapterContext(
            run_id=run_id,
            graph_run_id=run_id,
            initial_seq=1,
            checkpoint_ref=adapter_checkpoint_ref,
        )
        adapter = LangGraphEventAdapter()
        reducer = StreamReducer()

        # --- build stream kwargs (v3 rejects stream_mode/subgraphs) ---
        stream_kwargs: dict[str, Any] = {"version": "v3", "config": config}
        if native_context and self._callable_accepts_keyword(self._agent.astream_events, "context"):
            stream_kwargs["context"] = native_context

        was_interrupted = False
        last_timestamp = started_at

        try:
            run_stream = await self._agent.astream_events(stream_input, **stream_kwargs)
            async for event in adapter.stream_run(run_stream, adapter_context):
                if isinstance(event, RunInterrupted):
                    was_interrupted = True
                    # Extract checkpoint from graph state and emit
                    # ContinuationCreated BEFORE RunInterrupted so downstream
                    # consumers (agui agent) can resolve the resumable
                    # checkpoint_id before processing the terminal interrupt.
                    try:
                        ckpt_state = self._agent.get_state(config)
                        ckpt_config = getattr(ckpt_state, "config", {}) or {}
                        ckpt_id = str(
                            (ckpt_config.get("configurable") or {}).get("checkpoint_id", "") or ""
                        )
                        if ckpt_id:
                            ckpt_ref = {
                                "thread_id": str(
                                    (ckpt_config.get("configurable") or {}).get(
                                        "thread_id", session_id
                                    )
                                ),
                                "checkpoint_ns": "",
                                "checkpoint_id": ckpt_id,
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
                                    native_run_id=run_id,
                                    metadata={"checkpoint": True},
                                ),
                                continuation_id=continuation_id,
                                continuation_kind="graph_checkpoint",
                                resumable=True,
                                ref=ckpt_ref,
                            )
                            reducer.apply(cont_event)
                            yield cont_event
                    except Exception:
                        pass
                    reducer.apply(event)
                    last_timestamp = float(getattr(event, "timestamp", 0.0) or last_timestamp)
                    yield event
                    return
                reducer.apply(event)
                last_timestamp = float(getattr(event, "timestamp", 0.0) or last_timestamp)
                yield event
        except Exception as exc:
            error_source = SourceRef(
                framework="langgraph",
                native_run_id=run_id,
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
            native_run_id=run_id,
            metadata=dict(source_metadata),
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


__all__ = ["_LangGraphStreamMixin"]
