"""End a failed tunnel turn with a deterministic answer, without re-entering tools."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .protocol import TunnelError


@dataclass(frozen=True)
class TunnelFailure:
    indeterminate: bool
    result_code: str = "tunnel_tool_failed"

    def for_unanswered_calls(self, count: int) -> TunnelFailure:
        # Several missing responses cannot be attributed to the one raised
        # exception. A parallel sibling may already have completed a write.
        return TunnelFailure(True, "tool_result_unavailable") if count > 1 else self

    @property
    def tool_result(self) -> dict:
        return {
            "error": self.result_code,
            "message": self.message,
            "indeterminate": self.indeterminate,
        }

    @property
    def message(self) -> str:
        if self.indeterminate:
            return (
                "工具调用中断，结果未知，操作可能已执行。已停止本次操作，未自动重试；"
                "请先核实业务状态，再决定是否重新执行。"
            )
        return "工具调用失败，已停止本次操作，未自动重试。请检查工具服务或连接状态。"

    @property
    def metadata(self) -> dict:
        return {
            "tunnel_failure": {
                "outcome": "unknown" if self.indeterminate else "failed",
                "automatic_retry": False,
            }
        }


def tunnel_failure(exc: BaseException) -> TunnelFailure | None:
    """Handle only tunnel failures, never mask unrelated errors or cancellation."""
    if isinstance(exc, TunnelError):
        if exc.code == "tool_result_unavailable":
            return TunnelFailure(True, exc.code)
        return TunnelFailure(exc.indeterminate)
    if isinstance(exc, ExceptionGroup):
        failures = [tunnel_failure(error) for error in exc.exceptions]
        if failures and all(failure is not None for failure in failures):
            return TunnelFailure(
                any(failure.indeterminate for failure in failures if failure is not None)
            )
    return None


def failure_events(
    failure: TunnelFailure,
    *,
    run_id: str,
    scope_id: str,
    source,
    allocate_seq: Callable[[], int],
    open_items: Iterable[tuple[str, str, str]] = (),
    pending_calls: Iterable[tuple[str, str]] = (),
):
    """Keep failed items failed; complete only the assistant's failure notification.

    No graph/model is called, and no native checkpoint continuation is advertised.
    """
    from ksadk.events.canonical import (
        ErrorInfo,
        ItemCompleted,
        ItemFailed,
        ItemStarted,
        OutputRef,
        RunCompleted,
    )
    from ksadk.events.content import ContentSnapshot, TextContent, ToolResultContent
    from ksadk.events.identity import stable_event_id, stable_item_id

    pending_calls = tuple(pending_calls)
    failure = failure.for_unanswered_calls(len(pending_calls))
    source = source.model_copy(update={"metadata": {**source.metadata, **failure.metadata}})

    def envelope(event_type, item_id):
        return dict(
            schema_version=2,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            event_id=stable_event_id(
                source.framework, scope_id, item_id, event_type, "tunnel-failure", run_id, 0
            ),
            seq=allocate_seq(),
            timestamp=time.time(),
        )

    for item_scope, item_id, kind in open_items:
        fields = envelope("item.failed", item_id)
        fields["scope_id"] = item_scope
        yield ItemFailed(
            **fields,
            item_id=item_id,
            item_kind=kind,
            error=ErrorInfo(
                code="tunnel_tool_failed",
                message=failure.message,
                source=source.framework,
                scope_id=item_scope,
                item_id=item_id,
            ),
        )
    for item_scope, call_id in pending_calls:
        item_id = stable_item_id(source.framework, run_id, item_scope, call_id, "tunnel-failure")
        fields = envelope("item.started", item_id)
        fields["scope_id"] = item_scope
        yield ItemStarted(**fields, item_id=item_id, item_kind="tool_result")
        fields = envelope("item.completed", item_id)
        fields["scope_id"] = item_scope
        yield ItemCompleted(
            **fields,
            item_id=item_id,
            item_kind="tool_result",
            snapshot=ContentSnapshot(
                parts=(
                    ToolResultContent(
                        part_id="result",
                        call_id=call_id,
                        is_error=True,
                        result=failure.tool_result,
                    ),
                )
            ),
        )
    item_id = stable_item_id(source.framework, run_id, "tunnel-failure-answer")
    yield ItemStarted(
        **envelope("item.started", item_id),
        item_id=item_id,
        item_kind="message",
        phase="final_answer",
    )
    yield ItemCompleted(
        **envelope("item.completed", item_id),
        item_id=item_id,
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="answer", text=failure.message),)),
    )
    completed = RunCompleted(
        **envelope("run.completed", "run"),
        status="completed",
        output_refs=(OutputRef(scope_id=scope_id, item_id=item_id),),
    )
    if source.framework == "langgraph":
        from ksadk.events.native_checkpoint import NativeCheckpointCompleted

        # The notification completed; the failed graph did not reach a native
        # terminal snapshot. Never expose its pending tool task as resumable.
        completed = NativeCheckpointCompleted(**completed.model_dump())
        completed._checkpoint_error = True
    yield completed
