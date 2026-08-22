"""RunnerRuntimeAdapter 的 dict-chunk 退化路径:runner 流竞速与 chunk→canonical 事件映射。

从 ``ksadk.runtime.runner_adapter`` 按职责拆出(纯移动,行为不变)。以 mixin 形式
被 :class:`RunnerRuntimeAdapter` 继承,依赖宿主提供 ``_active_runs`` /
``_runtime_type`` / ``_runner`` / ``_next_seq`` / ``_canonical_kwargs`` /
``_interaction_requested_from_approval`` / ``_coerce``。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Mapping
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional, cast

from pydantic import JsonValue

from ksadk.conversations.runtime_input import _runner_name
from ksadk.conversations.runtime_observability import (
    _set_conversation_input_attributes,
    _set_conversation_output_attributes,
    _set_conversation_span_attributes,
    _set_conversation_usage_attributes,
)
from ksadk.events.canonical import (
    ApprovalRequest,
    ContentSnapshot,
    ContinuationCreated,
    ErrorInfo,
    EventEnvelope,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RunFailed,
    RunProgress,
    RuntimeEvent,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import DataContent, TextContent, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.runtime.adapter import RunHandle
from ksadk.runtime.preprocessing import PreparedRuntimeStart
from ksadk.runtime_context import platform_invocation_scope
from ksadk.tools.gateway import approval_interrupt_info_from_result

if TYPE_CHECKING:
    from ksadk.runtime.runner_adapter import _ActiveRun

logger = logging.getLogger(__name__)

_STREAM_STOP = object()


async def _anext_or_stop(gen: AsyncIterator[Any]) -> Any:
    """取下一个 chunk;流结束返回 _STREAM_STOP sentinel(便于竞速)。"""
    try:
        return await gen.__anext__()
    except StopAsyncIteration:
        return _STREAM_STOP


def _a2ui_surface_event(
    self: Any,
    handle: RunHandle,
    chunk: Any,
) -> RuntimeEvent | None:
    """Recognize a validated A2UI tool envelope and emit a canonical data-item event.

    The dynamic ``generate_a2ui`` tool returns official v0.9 operations as a
    JSON tool result. Tool results are otherwise opaque to the runtime, which
    would leave AG-UI with nothing to project until a page reload reconstructs
    history. Convert exactly that envelope at the runtime boundary so it is
    streamed, persisted, and replayed like every other A2UI surface.

    In the canonical schema, A2UI surfaces are modeled as ``item_kind="data"``
    items with ``source.protocol="a2ui"``.
    """

    if not isinstance(chunk, dict):
        return None
    value = chunk.get("tool_output", chunk.get("output"))
    if value is not None and hasattr(value, "content"):
        value = value.content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, Mapping):
        return None
    operations_raw = value.get("a2ui_operations")
    if not isinstance(operations_raw, list) or not operations_raw:
        return None
    operations = [dict(operation) for operation in operations_raw if isinstance(operation, Mapping)]
    if not operations:
        return None

    known: list[tuple[str, str]] = []  # (surface_id, lifecycle)
    for operation in operations:
        for key, lifecycle in (
            ("createSurface", "begin"),
            ("updateComponents", "update"),
            ("updateDataModel", "update"),
            ("deleteSurface", "end"),
        ):
            detail = operation.get(key)
            if isinstance(detail, Mapping) and isinstance(detail.get("surfaceId"), str):
                surface_id = detail["surfaceId"].strip()
                if surface_id:
                    known.append((surface_id, lifecycle))
                    break
    if not known:
        return None
    surface_ids = {surface_id for surface_id, _lifecycle in known}
    if len(surface_ids) != 1:
        logger.warning("ignoring A2UI tool result with multiple surfaces")
        return None
    surface_id = known[0][0]
    lifecycle = "begin" if any(lc == "begin" for _, lc in known) else known[0][1]

    framework = self._runtime_type
    run_id = handle.run_id
    scope_id = stable_scope_id(framework, run_id)
    item_id = stable_item_id(framework, run_id, "a2ui", surface_id)
    source = SourceRef(
        framework=framework,
        protocol="a2ui",
        native_run_id=run_id,
        metadata={"surface_id": surface_id},
    )
    # TODO(runtime-event-v2): dict chunk 退化路径,chunk_ordinal 用 seq counter;
    # LangGraph/Codex 切 stream_canonical_events 后清理
    n = self._next_seq()
    timestamp = time.time()
    if lifecycle == "begin":
        return ItemStarted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, item_id, "item.started", "a2ui", run_id, n
            ),
            seq=n,
            timestamp=timestamp,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            item_id=item_id,
            item_kind="data",
            initial=ContentSnapshot(parts=(DataContent(part_id="a2ui-ops", data=operations),)),
        )
    if lifecycle == "update":
        return ItemUpdated(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, item_id, "item.updated", "a2ui", run_id, n
            ),
            seq=n,
            timestamp=timestamp,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            item_id=item_id,
            item_kind="data",
            op="replace",
            update=DataContent(part_id="a2ui-ops", data=operations),
        )
    # lifecycle == "end"
    return ItemCompleted(
        schema_version=2,
        event_id=stable_event_id(framework, scope_id, item_id, "item.completed", "a2ui", run_id, n),
        seq=n,
        timestamp=timestamp,
        run_id=run_id,
        scope_id=scope_id,
        source=source,
        item_id=item_id,
        item_kind="data",
        snapshot=ContentSnapshot(parts=()),
    )


class _RunnerStreamMappingMixin:
    """``_map_runner_stream`` / ``_chunk_to_event`` 的实现载体(纯移动自 runner_adapter)。"""

    async def _map_runner_stream(
        self, handle: RunHandle, runner_input: dict
    ) -> AsyncIterator[RuntimeEvent]:
        run: Optional[_ActiveRun] = self._active_runs.get(handle.run_id)  # type: ignore[attr-defined]
        interrupt = run.interrupt_event if run is not None else None
        prepared_start = run.__dict__.get("_prepared_start") if run is not None else None
        invocation_context = (
            prepared_start.context if isinstance(prepared_start, PreparedRuntimeStart) else None
        )
        scope = (
            platform_invocation_scope(invocation_context)
            if invocation_context is not None
            else nullcontext()
        )
        runner_name = _runner_name(self._runner)  # type: ignore[attr-defined]
        accumulated_output = ""
        usage: dict[str, Any] = {}
        runner_gen: Optional[AsyncIterator[Any]] = None
        # span scope 经 runner_adapter 模块属性间接解析,保持既有 monkeypatch
        # patch 点(tests/agui/test_runtime_preprocessing.py)继续生效。
        from ksadk.runtime import runner_adapter as _runner_adapter_module

        async with _runner_adapter_module._conversation_span_scope(runner_name) as span:
            if isinstance(prepared_start, PreparedRuntimeStart):
                _set_conversation_span_attributes(
                    span,
                    agent_id=str(handle.native_ref.get("agent_id") or "agent"),
                    user_id=str(handle.native_ref.get("user_id") or "user"),
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    runner_name=runner_name,
                    model=prepared_start.context.model,
                    response_id=prepared_start.response_id,
                )
                _set_conversation_input_attributes(span, prepared_start.input_text)
            try:
                with scope:
                    canonical_stream = getattr(self._runner, "stream_canonical_events", None)  # type: ignore[attr-defined]
                    # ToolGateway 语义续跑 runner(gateway approval 可能出现在终态
                    # tool result 之后)仍走 chunk 路径:approval 识别逻辑在
                    # _chunk_to_events 的 tool_result 分支,canonical 快速路径
                    # (stream_canonical_events)不覆盖该语义。
                    if getattr(
                        self._runner, "supports_gateway_approval_semantic_resume", False  # type: ignore[attr-defined]
                    ):
                        canonical_stream = None
                    stream_result = (
                        canonical_stream(runner_input)
                        if callable(canonical_stream)
                        else self._runner.stream(runner_input)  # type: ignore[attr-defined]
                    )
                    if inspect.iscoroutine(stream_result):
                        # runner.stream 若声明为 async def -> AsyncIterator(非 async generator),
                        # 调用返回 coroutine,需 await 得到迭代器。
                        stream_result = await stream_result
                    runner_gen = cast(AsyncIterator[Any], stream_result)
                    while True:
                        # 竞速:下一个 runner chunk vs cancel 中断事件。
                        chunk_task = asyncio.ensure_future(_anext_or_stop(runner_gen))
                        if run is not None:
                            run.chunk_task = chunk_task
                        wait_set = {chunk_task}
                        interrupt_task = (
                            asyncio.ensure_future(interrupt.wait())
                            if interrupt is not None
                            else None
                        )
                        if interrupt_task is not None:
                            wait_set.add(interrupt_task)
                        done, pending = await asyncio.wait(
                            wait_set, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in pending:
                            task.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        if interrupt_task is not None and interrupt_task in done:
                            # cancel 中断:安全关闭 runner 流(同一 task)并停止。
                            chunk_task.cancel()
                            await asyncio.gather(chunk_task, return_exceptions=True)
                            return
                        try:
                            chunk = chunk_task.result()
                        except asyncio.CancelledError:
                            if interrupt is not None and interrupt.is_set():
                                return
                            raise
                        finally:
                            if run is not None:
                                run.chunk_task = None
                        if chunk is _STREAM_STOP:
                            return
                        if isinstance(chunk, EventEnvelope):
                            # canonical 事件(来自 stream_canonical_events):直接转发,
                            # 追踪 output/usage 供 span 属性。
                            if isinstance(chunk, ItemCompleted) and chunk.item_kind == "message":
                                accumulated_output = "".join(
                                    part.text
                                    for part in chunk.snapshot.parts
                                    if isinstance(part, TextContent)
                                )
                            elif isinstance(chunk, UsageReported):
                                usage.update(
                                    {
                                        "input_tokens": chunk.input_tokens,
                                        "output_tokens": chunk.output_tokens,
                                        "total_tokens": chunk.total_tokens,
                                        "cached_tokens": chunk.cached_tokens,
                                        "reasoning_tokens": chunk.reasoning_tokens,
                                    }
                                )
                        if isinstance(chunk, dict):
                            chunk_type = str(chunk.get("type") or "")
                            if chunk_type == "final" and run is not None:
                                for source_key, target_key in (
                                    ("duration_ms", "duration_ms"),
                                    ("started_at", "started_at"),
                                    ("completed_at", "completed_at"),
                                    ("metrics_source", "source"),
                                ):
                                    if chunk.get(source_key) is not None:
                                        run.completion_metrics[target_key] = chunk[source_key]
                            if chunk_type in {"final", "text", "text_delta"}:
                                text = self._coerce(  # type: ignore[attr-defined]
                                    chunk.get("delta") or chunk.get("output") or chunk.get("data")
                                )
                                if text:
                                    if chunk_type == "final" or chunk.get("replace"):
                                        accumulated_output = text
                                    else:
                                        accumulated_output += text
                            raw_usage = chunk.get("usage")
                            if isinstance(raw_usage, dict):
                                usage.update(raw_usage)
                        for event in self._chunk_to_event(handle, run, chunk):  # type: ignore[attr-defined]
                            yield event
                        a2ui_surface = _a2ui_surface_event(self, handle, chunk)
                        if a2ui_surface is not None:
                            yield a2ui_surface
            finally:
                if accumulated_output:
                    _set_conversation_output_attributes(span, accumulated_output)
                _set_conversation_usage_attributes(span, usage)
                if runner_gen is not None:
                    aclose = getattr(runner_gen, "aclose", None)
                    if callable(aclose):
                        try:
                            await aclose()
                        except Exception:  # noqa: BLE001
                            pass
                if run is not None:
                    run.cancellation_ack.set()

    def _chunk_to_event(
        self, handle: RunHandle, run: Optional[_ActiveRun], chunk: Any
    ) -> list[RuntimeEvent]:
        if isinstance(chunk, EventEnvelope):
            # canonical 事件(来自 stream_canonical_events):直接转发,
            # 抑制 runner 自己的 run.started(adapter 已发自己的)。
            if chunk.event_type == "run.started":
                return []
            return [chunk]
        if not isinstance(chunk, dict):
            chunk = {"type": "text", "delta": str(chunk)}

        framework = self._runtime_type  # type: ignore[attr-defined]
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        started = run.started_items if run is not None else set()

        def ensure_started(
            *,
            item_id: str,
            item_kind: str,
            phase: str | None = None,
            initial: ContentSnapshot | None = None,
        ) -> list[RuntimeEvent]:
            key = (scope_id, item_id)
            if key in started:
                return []
            started.add(key)
            return [
                ItemStarted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id="item",
                    ),
                    item_id=item_id,
                    item_kind=item_kind,
                    phase=phase,
                    initial=initial,
                )
            ]

        chunk_type = chunk.get("type")

        # ---- reasoning ----
        if chunk_type in ("reasoning", "reasoning_delta", "thinking"):
            text = self._coerce(  # type: ignore[attr-defined]
                chunk.get("delta")
                or chunk.get("content")
                or chunk.get("output")
                or chunk.get("data")
            )
            if not text:
                return []
            item_id = stable_item_id(framework, run_id, "reasoning")
            events: list[RuntimeEvent] = ensure_started(
                item_id=item_id, item_kind="reasoning", phase="commentary"
            )
            if chunk.get("status") in ("completed", "done"):
                events.append(
                    ItemCompleted(
                        **self._canonical_kwargs(  # type: ignore[attr-defined]
                            handle,
                            scope_id=scope_id,
                            item_id=item_id,
                            event_type="item.completed",
                            part_id="reasoning-text",
                        ),
                        item_id=item_id,
                        item_kind="reasoning",
                        snapshot=ContentSnapshot(
                            parts=(TextContent(part_id="reasoning-text", text=text),)
                        ),
                    )
                )
            else:
                events.append(
                    ItemUpdated(
                        **self._canonical_kwargs(  # type: ignore[attr-defined]
                            handle,
                            scope_id=scope_id,
                            item_id=item_id,
                            event_type="item.updated",
                            part_id="reasoning-text",
                        ),
                        item_id=item_id,
                        item_kind="reasoning",
                        op="append",
                        update=TextContent(part_id="reasoning-text", text=text),
                    )
                )
            return events

        # ---- tool_call ----
        if chunk_type in ("tool_call", "tool_start"):
            call_id = str(
                chunk.get("tool_call_id")
                or chunk.get("call_id")
                or chunk.get("run_id")
                or chunk.get("id")
                or ""
            )
            name = str(chunk.get("tool_name") or chunk.get("name") or "tool")
            effective_call_id = call_id or name
            item_id = stable_item_id(framework, run_id, effective_call_id, "tool_call")
            part_id = "tool_call"
            tc_content = ToolCallContent(
                part_id=part_id,
                call_id=effective_call_id,
                name=name,
                arguments=cast(JsonValue, chunk.get("tool_args", chunk.get("args")) or {}),
            )
            return [
                ItemStarted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id=part_id,
                    ),
                    item_id=item_id,
                    item_kind="tool_call",
                    phase="commentary",
                    initial=ContentSnapshot(parts=(tc_content,)),
                ),
                ItemCompleted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.completed",
                        part_id=part_id,
                    ),
                    item_id=item_id,
                    item_kind="tool_call",
                    snapshot=ContentSnapshot(parts=(tc_content,)),
                ),
            ]

        # ---- tool_result ----
        if chunk_type in ("tool_result", "tool_end"):
            call_id = str(
                chunk.get("tool_call_id")
                or chunk.get("call_id")
                or chunk.get("run_id")
                or chunk.get("id")
                or ""
            )
            name = str(chunk.get("tool_name") or chunk.get("name") or "tool")
            effective_call_id = call_id or name
            item_id = stable_item_id(framework, run_id, effective_call_id, "tool_result")
            part_id = "tool_result"
            result_data = chunk.get("tool_output", chunk.get("output"))
            # ToolGateway 审批可能出现在"本已终态"的 tool result 里;识别后转为
            # canonical InteractionRequested(语义续跑由 runner 的
            # supports_gateway_approval_semantic_resume 决定)。
            tool_args = chunk.get("tool_args", chunk.get("args"))
            approval_detail = approval_interrupt_info_from_result(
                result_data,
                fallback_tool_name=name,
                tool_args=tool_args,
                run_id=call_id or None,
            )
            if approval_detail is not None:
                return self._interaction_requested_from_approval(  # type: ignore[attr-defined]
                    handle,
                    run,
                    detail=approval_detail,
                    call_id=call_id,
                )
            tr_content = ToolResultContent(
                part_id=part_id,
                call_id=effective_call_id,
                result=cast(JsonValue, result_data if result_data is not None else {}),
                is_error=bool(chunk.get("error")),
            )
            return [
                ItemStarted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id=part_id,
                    ),
                    item_id=item_id,
                    item_kind="tool_result",
                    phase="commentary",
                ),
                ItemCompleted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.completed",
                        part_id=part_id,
                    ),
                    item_id=item_id,
                    item_kind="tool_result",
                    snapshot=ContentSnapshot(parts=(tr_content,)),
                ),
            ]

        # ---- interrupt / approval ----
        if chunk_type in ("interrupt", "approval", "approval_required"):
            detail = chunk.get("interrupt_info") or chunk.get("detail") or {}
            detail_id = detail.get("approval_request_id") if isinstance(detail, dict) else None
            call_id = str(
                chunk.get("call_id")
                or chunk.get("approval_id")
                or chunk.get("id")
                or detail_id
                or ""
            )
            if run is not None and call_id:
                run.pending_approvals.add(call_id)
            if call_id:
                pending_approval_ids = handle.native_ref.setdefault("pending_approval_ids", [])
                if call_id not in pending_approval_ids:
                    pending_approval_ids.append(call_id)
            interaction_id = call_id or stable_item_id(framework, run_id, "interaction")
            item_id = stable_item_id(framework, run_id, "interaction")
            detail_value: JsonValue = (
                cast(JsonValue, detail)
                if isinstance(detail, (dict, list, str, int, float, bool, type(None)))
                else None
            )
            return [
                InteractionRequested(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="interaction.requested",
                        part_id="interaction",
                    ),
                    interaction_id=interaction_id,
                    interaction_kind="approval",
                    request=ApprovalRequest(
                        call_id=call_id or None,
                        kind="tool",
                        detail=detail_value,
                    ),
                )
            ]

        # ---- checkpoint ----
        if chunk_type == "checkpoint":
            raw_metadata = chunk.get("metadata")
            metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
            raw_agentengine = metadata.get("agentengine")
            agentengine: dict[str, Any] = (
                raw_agentengine if isinstance(raw_agentengine, dict) else {}
            )
            ckpt_framework = str(agentengine.get("framework") or self._runtime_type)  # type: ignore[attr-defined]
            framework_ref = agentengine.get("framework_ref") or {}
            runtime_ref = (
                framework_ref.get(ckpt_framework) if isinstance(framework_ref, dict) else {}
            ) or {}
            checkpoint_id = str(
                runtime_ref.get("checkpoint_id") if isinstance(runtime_ref, dict) else ""
            )
            if not checkpoint_id:
                return []
            handle.native_ref["checkpoint_id"] = checkpoint_id
            known_checkpoint_ids = handle.native_ref.setdefault("known_checkpoint_ids", [])
            if checkpoint_id not in known_checkpoint_ids:
                known_checkpoint_ids.append(checkpoint_id)
            handle.native_ref["framework_ref"] = framework_ref
            if isinstance(runtime_ref, dict):
                handle.native_ref.update(runtime_ref)
            item_id = stable_item_id(framework, run_id, "$run")
            ref_value = cast(
                JsonValue,
                framework_ref if isinstance(framework_ref, dict) else {},
            )
            return [
                ContinuationCreated(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="continuation.created",
                        part_id="continuation",
                    ),
                    continuation_id=checkpoint_id,
                    continuation_kind="graph_checkpoint",
                    resumable=True,
                    ref={
                        "framework": ckpt_framework,
                        "framework_ref": ref_value,
                        "resume_target": ref_value,
                    },
                )
            ]

        # ---- graph_update ----
        if chunk_type == "graph_update":
            item_id = stable_item_id(framework, run_id, "$run")
            return [
                RunProgress(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="run.progress",
                        part_id="run",
                    ),
                    status="running",
                    message=str(chunk.get("node") or ""),
                )
            ]

        # ---- usage ----
        if chunk_type == "usage":
            raw_usage = chunk.get("usage")
            usage_dict: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            item_id = stable_item_id(framework, run_id, "$run")
            return [
                UsageReported(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="usage.reported",
                        part_id="usage",
                    ),
                    input_tokens=int(usage_dict.get("input_tokens") or 0),
                    output_tokens=int(usage_dict.get("output_tokens") or 0),
                    total_tokens=int(usage_dict.get("total_tokens") or 0),
                    cached_tokens=int(usage_dict.get("cached_tokens") or 0),
                    reasoning_tokens=int(usage_dict.get("reasoning_tokens") or 0),
                )
            ]

        # ---- error ----
        if chunk_type == "error":
            error = self._coerce(chunk.get("message") or chunk.get("error"))  # type: ignore[attr-defined]
            item_id = stable_item_id(framework, run_id, "$run")
            return [
                RunFailed(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="run.failed",
                        part_id="run",
                    ),
                    status="failed",
                    error=ErrorInfo(
                        code="runner_failed",
                        message=error or "runner failed",
                        source=framework,
                        scope_id=scope_id,
                    ),
                )
            ]

        # ---- final ----
        if chunk_type == "final":
            output = self._coerce(chunk.get("output"))  # type: ignore[attr-defined]
            item_id = stable_item_id(framework, run_id, "message", "final_answer")
            if run is not None:
                run.final_answer_item_id = item_id
            text_content = TextContent(part_id="text-0", text=output)
            # Auto-close any open commentary/reasoning item before emitting final_answer.
            # Text/thinking deltas create items that are never ItemCompleted;
            # without this, RunCompleted fails _ensure_no_open_items.
            #
            # Close them *before* allocating the final-answer item.  Event
            # constructors allocate ``seq`` eagerly, so inserting a later
            # completion at index zero would otherwise return an event list
            # whose physical order disagrees with its sequence numbers.
            events: list[RuntimeEvent] = []
            for close_kind, close_part_id, close_components in (
                ("message", "text-0", ("message", "commentary")),
                ("reasoning", "reasoning-text", ("reasoning",)),
            ):
                close_item_id = stable_item_id(framework, run_id, *close_components)
                close_key = (scope_id, close_item_id)
                if close_key in started:
                    started.discard(close_key)
                    events.append(
                        ItemCompleted(
                            **self._canonical_kwargs(  # type: ignore[attr-defined]
                                handle,
                                scope_id=scope_id,
                                item_id=close_item_id,
                                event_type="item.completed",
                                part_id=close_part_id,
                            ),
                            item_id=close_item_id,
                            item_kind=close_kind,
                            snapshot=ContentSnapshot(
                                parts=(TextContent(part_id=close_part_id, text=""),)
                            ),
                        ),
                    )
            events.extend(
                ensure_started(item_id=item_id, item_kind="message", phase="final_answer")
            )
            events.append(
                ItemCompleted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.completed",
                        part_id="text-0",
                    ),
                    item_id=item_id,
                    item_kind="message",
                    snapshot=ContentSnapshot(parts=(text_content,)),
                )
            )
            return events

        # ---- default: text delta ----
        text = self._coerce(chunk.get("delta") or chunk.get("output") or chunk.get("data"))  # type: ignore[attr-defined]
        if not text:
            return []
        item_id = stable_item_id(framework, run_id, "message", "commentary")
        op: str = "replace" if chunk.get("replace") else "append"
        events = ensure_started(item_id=item_id, item_kind="message", phase="commentary")
        events.append(
            ItemUpdated(
                **self._canonical_kwargs(  # type: ignore[attr-defined]
                    handle,
                    scope_id=scope_id,
                    item_id=item_id,
                    event_type="item.updated",
                    part_id="text-0",
                ),
                item_id=item_id,
                item_kind="message",
                op=op,
                update=TextContent(part_id="text-0", text=text),
            )
        )
        return events


__all__ = ["_RunnerStreamMappingMixin", "_a2ui_surface_event", "_anext_or_stop", "_STREAM_STOP"]
