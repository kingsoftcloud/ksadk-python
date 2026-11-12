"""RunnerRuntimeAdapter 的 dict-chunk 退化路径:runner 流竞速与 chunk→canonical 事件映射。

从 ``ksadk.runtime.runner_adapter`` 按职责拆出(纯移动,行为不变)。以 mixin 形式
被 :class:`RunnerRuntimeAdapter` 继承,依赖宿主提供 ``_active_runs`` /
``_runtime_type`` / ``_runner`` / ``_next_seq`` / ``_canonical_kwargs`` /
``_interaction_requested_from_approval`` / ``_coerce``。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import traceback
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional, cast

from ksadk.common.diagnostics import safe_exception_detail
from ksadk.conversations.runtime_input import _runner_name
from ksadk.conversations.runtime_observability import (
    _set_conversation_input_attributes,
    _set_conversation_output_attributes,
    _set_conversation_span_attributes,
    _set_conversation_usage_attributes,
    _set_skill_eval_result_attributes,
)
from ksadk.events.canonical import (
    ErrorInfo,
    EventEnvelope,
    ItemCompleted,
    RunCompleted,
    RunFailed,
    RuntimeEvent,
    UsageReported,
)
from ksadk.events.content import TextContent
from ksadk.events.identity import stable_item_id, stable_scope_id
from ksadk.events.usage import apply_usage_report
from ksadk.runtime._runner_adapter.chunk_mapping import RunnerChunkMappingMixin, _a2ui_surface_event
from ksadk.runtime._runner_adapter.a2ui_surface import bind_legacy_input_surface
from ksadk.runtime.adapter import RunHandle
from ksadk.runtime.preprocessing import PreparedRuntimeStart
from ksadk.runtime.skill_eval_result import extract_skill_eval_result
from ksadk.runtime.timing import extract_timing
from ksadk.runtime_context import platform_invocation_scope

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


async def _pump_stream_in_one_context(
    gen: AsyncIterator[Any],
    requests: asyncio.Queue[None],
    results: asyncio.Queue[tuple[str, Any]],
) -> None:
    """Own one async generator in one Task without reading ahead."""
    outcome: tuple[str, Any] = ("stop", _STREAM_STOP)
    try:
        while True:
            await requests.get()
            try:
                chunk = await gen.__anext__()
            except StopAsyncIteration:
                break
            await results.put(("chunk", chunk))
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        outcome = ("error", exc)
    finally:
        aclose = getattr(gen, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if outcome[0] != "error":
                    outcome = ("error", exc)
    await results.put(outcome)




class _RunnerStreamMappingMixin(RunnerChunkMappingMixin):
    """``_map_runner_stream`` / ``_chunk_to_event`` 的实现载体(纯移动自 runner_adapter)。"""

    async def _map_runner_stream(
        self,
        handle: RunHandle,
        runner_input: dict,
        *,
        active_run: Optional[_ActiveRun] = None,
    ) -> AsyncIterator[RuntimeEvent]:
        run: Optional[_ActiveRun] = active_run
        if run is None:
            run = self._active_runs.get(handle.run_id)  # type: ignore[attr-defined]
        interrupt = run.interrupt_event if run is not None else None
        prepared_start = run.__dict__.get("_prepared_start") if run is not None else None
        invocation_context = (
            prepared_start.context if isinstance(prepared_start, PreparedRuntimeStart) else None
        )
        if run is not None and run.__dict__.get("_tag_context") is not None:
            tag_context = run.__dict__["_tag_context"]
            invocation_context = (
                replace(invocation_context, session=tag_context.session)
                if invocation_context is not None
                else tag_context
            )
        scope = (
            platform_invocation_scope(invocation_context)
            if invocation_context is not None
            else nullcontext()
        )
        runner_name = _runner_name(self._runner)  # type: ignore[attr-defined]
        accumulated_output = ""
        usage: dict[str, Any] = {}
        usage_event_ids: set[str] = set()
        runner_gen: Optional[AsyncIterator[Any]] = None
        pump_requests: asyncio.Queue[None] | None = None
        pump_queue: asyncio.Queue[tuple[str, Any]] | None = None
        pump_task: asyncio.Task[None] | None = None
        saw_terminal_event = False
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
                    # Keep legacy semantic-resume runners on their chunk path.
                    # Managed LangGraph requires canonical identity; its local
                    # gateway results are handled by _canonical_gateway_approvals.
                    if getattr(
                        self._runner,
                        "supports_gateway_approval_semantic_resume",
                        False,  # type: ignore[attr-defined]
                    ) and not (
                        self._runtime_type == "langgraph"
                        and (getattr(self._runner, "_managed_a2a_bootstrap", None) is not None
                             or getattr(self._runner, "prefers_canonical_stream", False))
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
                    if self._requires_stable_stream_context():
                        pump_requests = asyncio.Queue(maxsize=1)
                        pump_queue = asyncio.Queue()
                        pump_task = asyncio.create_task(
                            _pump_stream_in_one_context(
                                runner_gen, pump_requests, pump_queue
                            )
                        )
                        if run is not None:
                            run.chunk_task = pump_task
                    while True:
                        # 竞速:下一个 runner chunk vs cancel 中断事件。
                        if pump_requests is not None:
                            pump_requests.put_nowait(None)
                        chunk_task = asyncio.ensure_future(
                            pump_queue.get()
                            if pump_queue is not None
                            else _anext_or_stop(runner_gen)
                        )
                        if run is not None and pump_task is None:
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
                            owner_task = pump_task or chunk_task
                            owner_task.cancel()
                            await asyncio.gather(owner_task, return_exceptions=True)
                            return
                        try:
                            chunk = chunk_task.result()
                            if pump_task is not None:
                                outcome, payload = cast(tuple[str, Any], chunk)
                                if outcome == "error":
                                    raise payload
                                chunk = payload
                        except asyncio.CancelledError:
                            if interrupt is not None and interrupt.is_set():
                                return
                            raise
                        finally:
                            if run is not None and pump_task is None:
                                run.chunk_task = None
                        if chunk is _STREAM_STOP:
                            return
                        if isinstance(chunk, EventEnvelope):
                            # canonical 事件(来自 stream_canonical_events):直接转发,
                            # 追踪 output/usage 供 span 属性。
                            if isinstance(chunk, RunCompleted):
                                evidence = extract_skill_eval_result(
                                    chunk.source.metadata.get("metrics")
                                )
                                if evidence:
                                    _set_skill_eval_result_attributes(span, evidence)
                            if isinstance(chunk, ItemCompleted) and chunk.item_kind == "message":
                                accumulated_output = "".join(
                                    part.text
                                    for part in chunk.snapshot.parts
                                    if isinstance(part, TextContent)
                                )
                            elif (
                                isinstance(chunk, UsageReported)
                                and chunk.event_id not in usage_event_ids
                            ):
                                usage = apply_usage_report(usage, chunk)
                                usage_event_ids.add(chunk.event_id)
                        if isinstance(chunk, dict):
                            chunk_type = str(chunk.get("type") or "")
                            if chunk_type == "final" and run is not None:
                                if evidence := extract_skill_eval_result(chunk):
                                    run.completion_metrics["skill_eval_result"] = evidence
                                    _set_skill_eval_result_attributes(span, evidence)
                                if timing := extract_timing(chunk):
                                    run.completion_metrics["timing"] = timing
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
                        # The ledger must see the verified display before it persists the wait.
                        bind_legacy_input_surface(self, handle, chunk)
                        for event in self._chunk_to_event(handle, run, chunk):  # type: ignore[attr-defined]
                            if getattr(event, "event_type", "") in {
                                "run.completed",
                                "run.failed",
                                "run.canceled",
                                "run.interrupted",
                            }:
                                saw_terminal_event = True
                            yield event
                        for a2ui_surface in _a2ui_surface_event(self, handle, chunk):
                            yield a2ui_surface
            except Exception as exc:
                # Record locations only: exception strings and locals can contain
                # credentials or user tool payloads. Empty TimeoutError messages
                # otherwise make checkpoint/transport failures indistinguishable.
                logger.warning(
                    "Runner stream exception type=%s locations=%s",
                    type(exc).__name__,
                    " > ".join(f"{frame.name}:{frame.lineno}"
                               for frame in traceback.extract_tb(exc.__traceback__)),
                )
                # 原始 runner 异常(如 GraphRecursionError)不走 "error" chunk 路径。
                # 不转换则 canonical 事件流没有 run.failed 终态：下游按"流结束无终态"
                # 报错，会话 run 状态缺失 canonical failed，前端只见干净收尾。这里
                # 把异常转成 canonical RunFailed 后正常终止；runner 已自行发过终态
                # 时不重复。CancelledError 是 BaseException，不经此处，保持取消语义。
                if not saw_terminal_event:
                    framework = self._runtime_type  # type: ignore[attr-defined]
                    run_id = handle.run_id
                    scope_id = stable_scope_id(framework, run_id)
                    yield RunFailed(
                        **self._canonical_kwargs(  # type: ignore[attr-defined]
                            handle,
                            scope_id=scope_id,
                            item_id=stable_item_id(framework, run_id, "$run"),
                            event_type="run.failed",
                            part_id="run",
                        ),
                        status="failed",
                        error=ErrorInfo(
                            code="runner_failed",
                            message=safe_exception_detail(exc),
                            source=framework,
                            scope_id=scope_id,
                        ),
                    )
                return
            finally:
                if accumulated_output:
                    _set_conversation_output_attributes(span, accumulated_output)
                _set_conversation_usage_attributes(span, usage)
                if pump_task is not None:
                    if not pump_task.done():
                        pump_task.cancel()
                    await asyncio.gather(pump_task, return_exceptions=True)
                elif runner_gen is not None:
                    aclose = getattr(runner_gen, "aclose", None)
                    if callable(aclose):
                        try:
                            await aclose()
                        except Exception:  # noqa: BLE001
                            pass
                if run is not None:
                    if pump_task is not None:
                        run.chunk_task = None
                    run.cancellation_ack.set()



__all__ = ["_RunnerStreamMappingMixin", "_a2ui_surface_event", "_anext_or_stop", "_STREAM_STOP"]
