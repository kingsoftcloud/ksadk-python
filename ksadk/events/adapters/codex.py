"""Codex app-server 0.147.0 JSONL messages to RuntimeEvent schema v2."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable

from ksadk.events.adapters._codex_interactions import _CodexInteractionMixin
from ksadk.events.adapters._codex_items import (
    _CODEX_0_147_0_NOTIFICATION_METHODS,
    _CONTROL_INTERACTION_METHODS,
    _FAILURE_CODE_KINDS,
    _INTERACTION_METHODS,
    _ITEM_METHODS,
    CodexAdapterContext,
    _completed_snapshot,
    _envelope,
    _fail,
    _initial_snapshot,
    _InteractionState,
    _item_failed,
    _item_state,
    _item_update,
    _ItemState,
    _part_id,
    _protocol_source,
    _ReplayRecord,
    _source,
    _thread_continuation_identity,
)
from ksadk.events.adapters._codex_validators import (
    CodexMappingError as CodexMappingError,  # noqa: F401
)
from ksadk.events.adapters._codex_validators import (
    _json_value,
    _mapping,
    _nonnegative_int,
    _request_id,
    _required_string,
    _required_text,
    _safe_codex_error_info_kind,
)
from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    ErrorInfo,
    InteractionRequested,
    ItemCompleted,
    ItemFailed,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    StructuredInputRequest,
    UsageReported,
)
from ksadk.events.content import (
    ContentSnapshot,
    DataContent,
)
from ksadk.events.identity import stable_item_id, stable_scope_id


class CodexEventAdapter(_CodexInteractionMixin):
    """Map one source-owned Codex JSONL frame at a time."""

    _REPLAY_WINDOW_LIMIT = 1024

    def __init__(self, *, known_thread_ids: Iterable[str] = ()) -> None:
        self._items: dict[tuple[str, str], _ItemState] = {}
        self._active_turns: set[str] = set()
        self._completed_items: dict[str, list[OutputRef]] = {}
        self._interactions: dict[str, _InteractionState] = {}
        self._thread_continuations: dict[str, str] = {
            thread_id: _thread_continuation_identity(thread_id)[1]
            for thread_id in known_thread_ids
            if thread_id
        }
        self._resume_requests: dict[str, str] = {}
        self._pending_resume_by_thread: dict[str, str] = {}
        self._replay_window: OrderedDict[str, _ReplayRecord] = OrderedDict()

    @property
    def replay_window_limit(self) -> int:
        """Maximum number of source mutation identities retained for replay safety."""

        return self._REPLAY_WINDOW_LIMIT

    @property
    def replay_window_size(self) -> int:
        """Current bounded replay identity count (exposed for diagnostics/tests)."""

        return len(self._replay_window)

    def map_protocol_message(
        self,
        message: Mapping[str, Any],
        context: CodexAdapterContext,
        *,
        native_cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        cursor = _required_string(native_cursor, "native_cursor")
        payload_digest = hashlib.sha256(
            json.dumps(
                _json_value(message),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        previous = self._replay_window.get(cursor)
        if previous is not None:
            if previous.payload_digest != payload_digest:
                _fail(
                    "native_event_collision",
                    "native_cursor",
                    f"Codex native cursor {cursor!r} was reused with a different payload",
                )
            self._replay_window.move_to_end(cursor)
            return ()
        shadow = copy.deepcopy(self)
        shadow_context = copy.deepcopy(context)
        events = shadow._map_protocol_message(
            message,
            shadow_context,
            cursor=cursor,
            timestamp=timestamp,
        )
        shadow._replay_window[cursor] = _ReplayRecord(
            payload_digest=payload_digest,
            event_ids=tuple(event.event_id for event in events),
        )
        while len(shadow._replay_window) > self._REPLAY_WINDOW_LIMIT:
            shadow._replay_window.popitem(last=False)
        self.__dict__.clear()
        self.__dict__.update(shadow.__dict__)
        context._next_seq = shadow_context._next_seq
        return events

    def _map_protocol_message(
        self,
        message: Mapping[str, Any],
        context: CodexAdapterContext,
        *,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        if "method" not in message:
            return self._map_jsonrpc_response(message, context, cursor, timestamp)

        method = _required_string(message.get("method"), "method")
        params = _mapping(message.get("params"), "params")
        if method == "thread/resume":
            request_id = _request_id(message.get("id"), "id")
            thread_id = _required_string(params.get("threadId"), "params.threadId")
            if thread_id in self._pending_resume_by_thread:
                _fail(
                    "thread_resume_already_pending",
                    "params.threadId",
                    f"Codex thread {thread_id!r} already has a pending resume",
                )
            self._resume_requests[request_id] = thread_id
            self._pending_resume_by_thread[thread_id] = request_id
            return ()

        if method in {"turn/started", "turn/completed"}:
            return self._map_turn_event(
                method=method,
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        if method == "thread/tokenUsage/updated":
            return self._map_token_usage(
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        if method == "serverRequest/resolved":
            return self._map_server_request_resolved(
                params=params, context=context, cursor=cursor, timestamp=timestamp
            )
        if method == "a2ui/surface":
            return self._map_a2ui_surface(
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        if method == "a2ui/interaction":
            return self._map_a2ui_interaction(
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        if method in _CONTROL_INTERACTION_METHODS:
            return self._map_control_interaction_request(
                message=message,
                method=method,
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        if method in _ITEM_METHODS or method in _INTERACTION_METHODS:
            thread_id = _required_string(params.get("threadId"), "params.threadId")
            turn_value = params.get("turnId")
            interrupts_run = not (method == "mcpServer/elicitation/request" and turn_value is None)
            turn_id = (
                _required_string(turn_value, "params.turnId")
                if interrupts_run
                else "mcp_elicitation"
            )
            scope_id = stable_scope_id("codex", thread_id, turn_id)
            env = _envelope(context, cursor, timestamp)

            if method == "error":
                return self._map_error(
                    params,
                    env,
                    scope_id=scope_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    cursor=cursor,
                )
            if method == "item/started":
                return self._map_item_started(
                    params,
                    env,
                    scope_id=scope_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    cursor=cursor,
                )
            if method == "item/completed":
                return self._map_item_terminal(
                    method, params, env, scope_id=scope_id, cursor=cursor
                )
            if method in _ITEM_METHODS:
                return self._map_item_updated(method, params, env, scope_id=scope_id, cursor=cursor)
            return self._map_interaction_request(
                message=message,
                method=method,
                params=params,
                env=env,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
                thread_id=thread_id,
                turn_id=turn_id,
                scope_id=scope_id,
                interrupts_run=interrupts_run,
            )
        if method in _CODEX_0_147_0_NOTIFICATION_METHODS:
            return self._map_known_notification(
                method=method, params=params, context=context, cursor=cursor, timestamp=timestamp
            )
        _fail("unsupported_method", "method", f"Unsupported Codex app-server method: {method}")

    @staticmethod
    def _a2ui_scope(
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        *,
        surface_id: str,
    ) -> tuple[str, str, str]:
        thread_value = params.get("threadId", params.get("thread_id"))
        turn_value = params.get("turnId", params.get("turn_id"))
        thread_id = (
            _required_string(thread_value, "params.threadId")
            if thread_value is not None
            else f"runtime:{context.run_id}"
        )
        turn_id = (
            _required_string(turn_value, "params.turnId")
            if turn_value is not None
            else "a2ui"
        )
        return thread_id, turn_id, stable_scope_id("codex", thread_id, turn_id, surface_id)

    @staticmethod
    def _a2ui_source(
        *,
        method: str,
        cursor: str,
        thread_id: str,
        turn_id: str,
        native_item_id: str,
        surface_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceRef:
        source = _protocol_source(
            method=method,
            cursor=cursor,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=native_item_id,
            native_event_id=native_item_id,
        )
        return source.model_copy(
            update={
                "protocol": "a2ui",
                "metadata": {
                    **source.metadata,
                    "surface_id": surface_id,
                    **dict(metadata or {}),
                },
            }
        )

    def _map_a2ui_surface(
        self,
        *,
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        """Map one complete A2UI surface description as an immutable operation batch."""

        surface_id = _required_string(params.get("surface_id"), "params.surface_id")
        surface = _mapping(params.get("surface"), "params.surface")
        thread_id, turn_id, scope_id = self._a2ui_scope(
            params, context, surface_id=surface_id
        )
        item_id = stable_item_id("codex", scope_id, "a2ui-surface", surface_id)
        source = self._a2ui_source(
            method="a2ui/surface",
            cursor=cursor,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=surface_id,
            surface_id=surface_id,
            metadata={
                "operation_batch": True,
                "surface_lifecycle": "begin",
                "catalog_id": str(surface.get("catalog_id") or surface.get("catalogId") or ""),
            },
        )
        snapshot = ContentSnapshot(
            parts=(
                DataContent(
                    part_id="a2ui-surface",
                    data={"surface_id": surface_id, **_json_value(surface)},
                ),
            )
        )
        env = _envelope(context, cursor, timestamp)
        return (
            ItemStarted(
                **env(scope_id, item_id, "item.started", "a2ui-surface", source),
                item_id=item_id,
                item_kind="data",
                initial=snapshot,
            ),
            ItemCompleted(
                **env(scope_id, item_id, "item.completed", "a2ui-surface", source),
                item_id=item_id,
                item_kind="data",
                snapshot=snapshot,
            ),
        )

    def _map_a2ui_interaction(
        self,
        *,
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        """Map the client-owned A2UI input request without changing its live call id."""

        surface_id = _required_string(params.get("surface_id"), "params.surface_id")
        interaction_id = _required_string(
            params.get("interaction_id"), "params.interaction_id"
        )
        kind = _required_string(params.get("kind"), "params.kind")
        schema = _mapping(params.get("input_schema"), "params.input_schema")
        is_blocking = params.get("is_blocking", True)
        if not isinstance(is_blocking, bool):
            _fail(
                "invalid_interaction_request",
                "params.is_blocking",
                "A2UI is_blocking must be a boolean",
            )
        thread_id, turn_id, scope_id = self._a2ui_scope(
            params, context, surface_id=surface_id
        )
        source = self._a2ui_source(
            method="a2ui/interaction",
            cursor=cursor,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=interaction_id,
            surface_id=surface_id,
            metadata={"kind": kind, "is_blocking": is_blocking},
        )
        env = _envelope(context, cursor, timestamp)
        requested = InteractionRequested(
            **env(
                scope_id,
                interaction_id,
                "interaction.requested",
                "structured_input",
                source,
            ),
            interaction_id=interaction_id,
            interaction_kind="structured_input",
            request=StructuredInputRequest(prompt=None, schema=_json_value(schema)),
        )
        if not is_blocking:
            return (requested,)
        return (
            requested,
            RunInterrupted(
                **env(
                    scope_id,
                    turn_id,
                    "run.interrupted",
                    interaction_id,
                    source,
                ),
                status="interrupted",
                reason="Codex requires user interaction",
                interaction_id=interaction_id,
                continuation_id=self._thread_continuations.setdefault(
                    thread_id, _thread_continuation_identity(thread_id)[1]
                ),
            ),
        )

    def _map_token_usage(
        self,
        *,
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        """Project the current turn's exact App Server usage into the canonical event."""

        thread_id = _required_string(params.get("threadId"), "params.threadId")
        turn_id = _required_string(params.get("turnId"), "params.turnId")
        usage_value = params.get("tokenUsage", params.get("token_usage"))
        usage = _mapping(usage_value, "params.tokenUsage")
        last = _mapping(usage.get("last"), "params.tokenUsage.last")

        def metric(camel_name: str, snake_name: str) -> int:
            value = last.get(camel_name, last.get(snake_name))
            return _nonnegative_int(value, f"params.tokenUsage.last.{camel_name}")

        input_tokens = metric("inputTokens", "input_tokens")
        output_tokens = metric("outputTokens", "output_tokens")
        total_tokens = metric("totalTokens", "total_tokens")
        cached_tokens = metric("cachedInputTokens", "cached_input_tokens")
        reasoning_tokens = metric("reasoningOutputTokens", "reasoning_output_tokens")
        scope_id = stable_scope_id("codex", thread_id, turn_id)
        source = _protocol_source(
            method="thread/tokenUsage/updated",
            cursor=cursor,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=None,
        )
        env = _envelope(context, cursor, timestamp)
        return (
            UsageReported(
                **env(scope_id, turn_id, "usage.reported", "usage", source),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cached_tokens=cached_tokens,
                reasoning_tokens=reasoning_tokens,
            ),
        )

    def finish_stream(self) -> None:
        """Fail closed if JSONL EOF leaves source-owned lifecycle state open."""

        if not (
            self._items
            or self._active_turns
            or self._interactions
            or self._pending_resume_by_thread
        ):
            return
        _fail(
            "open_state_at_stream_end",
            "jsonl eof",
            "Codex JSONL ended with open items, turns, interactions, or resumes",
        )

    def _map_known_notification(
        self,
        *,
        method: str,
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        """Losslessly preserve legal 0.144.4 control notifications as typed data."""

        thread_id_value = params.get("threadId")
        turn_id_value = params.get("turnId")
        thread_value = params.get("thread")
        turn_value = params.get("turn")
        if thread_id_value is None and isinstance(thread_value, Mapping):
            thread_id_value = thread_value.get("id")
        if turn_id_value is None and isinstance(turn_value, Mapping):
            turn_id_value = turn_value.get("id")
        thread_id = (
            _required_string(thread_id_value, "params.threadId")
            if thread_id_value is not None
            else f"runtime:{context.run_id}"
        )
        turn_id = (
            _required_string(turn_id_value, "params.turnId")
            if turn_id_value is not None
            else "control"
        )
        scope_id = stable_scope_id("codex", thread_id, turn_id)
        state = _ItemState(
            scope_id=scope_id,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=f"{method}:{cursor}",
            native_item_kind="notification",
            item_id=stable_item_id("codex", scope_id, "notification", method, cursor),
            item_kind="data",
            phase="commentary",
        )
        source = _source(method, cursor, state)
        part = DataContent(
            part_id=_part_id(state, "notification", "params"),
            data=_json_value(params),
        )
        env = _envelope(context, cursor, timestamp)
        return (
            ItemStarted(
                **env(scope_id, state.item_id, "item.started", "notification", source),
                item_id=state.item_id,
                item_kind="data",
                phase="commentary",
                initial=None,
            ),
            ItemCompleted(
                **env(scope_id, state.item_id, "item.completed", "snapshot", source),
                item_id=state.item_id,
                item_kind="data",
                snapshot=ContentSnapshot(parts=(part,)),
            ),
        )

    def _map_turn_event(
        self,
        *,
        method: str,
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        thread_id = _required_string(params.get("threadId"), "params.threadId")
        turn = _mapping(params.get("turn"), "params.turn")
        turn_id = _required_string(turn.get("id"), "params.turn.id")
        status = _required_string(turn.get("status"), "params.turn.status")
        scope_id = stable_scope_id("codex", thread_id, turn_id)
        source = _protocol_source(
            method=method,
            cursor=cursor,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=None,
        )
        env = _envelope(context, cursor, timestamp)

        if method == "turn/started":
            return self._map_turn_started(
                env=env,
                source=source,
                thread_id=thread_id,
                turn_id=turn_id,
                scope_id=scope_id,
                status=status,
                cursor=cursor,
            )

        if scope_id not in self._active_turns:
            _fail(
                "turn_not_started",
                "params.turn.id",
                f"Codex turn {turn_id!r} completed before turn/started",
            )
        open_items = sorted(
            state.native_item_id for state in self._items.values() if state.scope_id == scope_id
        )
        if open_items:
            _fail(
                "open_items_at_turn_end",
                "item/completed",
                f"Codex turn ended with open items: {open_items}",
            )
        output_refs = tuple(self._completed_items.get(scope_id, ()))
        items = turn.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            _fail("invalid_turn_snapshot", "params.turn.items", "Codex turn items must be an array")

        terminal: RuntimeEvent
        if status == "completed":
            terminal = RunCompleted(
                **env(scope_id, turn_id, "run.completed", "run", source),
                status="completed",
                output_refs=output_refs,
            )
        elif status == "failed":
            error = _mapping(turn.get("error"), "params.turn.error")
            message = _required_text(error.get("message"), "params.turn.error.message")
            terminal = RunFailed(
                **env(scope_id, turn_id, "run.failed", "run", source),
                status="failed",
                error=ErrorInfo(
                    code="codex_turn_failed",
                    message=message,
                    source="codex",
                    scope_id=scope_id,
                    source_ref=source,
                ),
            )
        elif status == "interrupted":
            terminal = RunCanceled(
                **env(scope_id, turn_id, "run.canceled", "run", source),
                status="canceled",
                reason="Codex turn/interrupt completed",
            )
        else:
            _fail(
                "invalid_turn_status",
                "params.turn.status",
                f"Unsupported terminal Codex turn status: {status}",
            )
        self._active_turns.remove(scope_id)
        self._completed_items.pop(scope_id, None)
        return (terminal,)

    def _map_turn_started(
        self,
        *,
        env: Callable[..., dict[str, Any]],
        source: SourceRef,
        thread_id: str,
        turn_id: str,
        scope_id: str,
        status: str,
        cursor: str,
    ) -> tuple[RuntimeEvent, ...]:
        if status != "inProgress":
            _fail(
                "invalid_turn_status",
                "params.turn.status",
                f"Codex turn/started requires inProgress, got: {status}",
            )
        if scope_id in self._active_turns:
            _fail("turn_already_started", "params.turn.id", f"Codex turn {turn_id!r} started twice")
        self._active_turns.add(scope_id)
        run_started = RunStarted(
            **env(scope_id, turn_id, "run.started", "run", source), status="running"
        )
        continuation_scope_id, derived_continuation_id = _thread_continuation_identity(thread_id)
        continuation_existed = thread_id in self._thread_continuations
        continuation_id = self._thread_continuations.setdefault(thread_id, derived_continuation_id)
        resume_attempt = self._pending_resume_by_thread.pop(thread_id, None)
        if resume_attempt is not None:
            self._resume_requests.pop(resume_attempt, None)
            continuation: RuntimeEvent = ContinuationResumed(
                **env(
                    continuation_scope_id,
                    continuation_id,
                    "continuation.resumed",
                    "thread_resume",
                    source,
                ),
                continuation_id=continuation_id,
                continuation_kind="thread_resume",
                resume_attempt_id=resume_attempt,
            )
        elif not continuation_existed:
            continuation = ContinuationCreated(
                **env(
                    continuation_scope_id,
                    continuation_id,
                    "continuation.created",
                    "thread_resume",
                    source,
                ),
                continuation_id=continuation_id,
                continuation_kind="thread_resume",
                resumable=True,
                ref={
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "source_cursor": cursor,
                },
            )
        else:
            self._completed_items.setdefault(scope_id, [])
            return (run_started,)
        self._completed_items.setdefault(scope_id, [])
        return (run_started, continuation)

    def _map_error(
        self,
        params: Mapping[str, Any],
        env: Callable[..., dict[str, Any]],
        *,
        scope_id: str,
        thread_id: str,
        turn_id: str,
        cursor: str,
    ) -> tuple[RuntimeEvent, ...]:
        error = _mapping(params.get("error"), "params.error")
        _required_text(error.get("message"), "params.error.message")
        will_retry = params.get("willRetry")
        if not isinstance(will_retry, bool):
            _fail(
                "invalid_protocol_message",
                "params.willRetry",
                "Codex params.willRetry must be a boolean",
            )
        base = _protocol_source(
            method="error",
            cursor=cursor,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=None,
        )
        source = base.model_copy(
            update={
                "metadata": {
                    **base.metadata,
                    "will_retry": will_retry,
                    "error_message_present": True,
                    "additional_details_present": error.get("additionalDetails") is not None,
                    "codex_error_info_present": error.get("codexErrorInfo") is not None,
                    "codex_error_info_kind": _safe_codex_error_info_kind(
                        error.get("codexErrorInfo")
                    ),
                }
            }
        )
        return (
            RunProgress(
                **env(
                    scope_id,
                    turn_id,
                    "run.progress",
                    "retryable_error" if will_retry else "error_diagnostic",
                    source,
                ),
                status="running",
                message=(
                    "Codex reported a retryable turn error"
                    if will_retry
                    else "Codex reported a non-retryable turn error"
                ),
            ),
        )

    def _map_item_started(
        self,
        params: Mapping[str, Any],
        env: Callable[..., dict[str, Any]],
        *,
        scope_id: str,
        thread_id: str,
        turn_id: str,
        cursor: str,
    ) -> tuple[RuntimeEvent, ...]:
        item = _mapping(params.get("item"), "params.item")
        native_item_id = _required_string(item.get("id"), "params.item.id")
        native_kind = _required_string(item.get("type"), "params.item.type")
        state = _item_state(scope_id, thread_id, turn_id, native_item_id, native_kind, item)
        key = (scope_id, native_item_id)
        if key in self._items:
            _fail(
                "item_already_started",
                "params.item.id",
                f"Codex item {native_item_id!r} started twice",
            )
        self._items[key] = state
        source = _source("item/started", cursor, state)
        return (
            ItemStarted(
                **env(scope_id, state.item_id, "item.started", "item", source),
                item_id=state.item_id,
                item_kind=state.item_kind,
                phase=state.phase,
                initial=_initial_snapshot(state, item),
            ),
        )

    def _map_item_updated(
        self,
        method: str,
        params: Mapping[str, Any],
        env: Callable[..., dict[str, Any]],
        *,
        scope_id: str,
        cursor: str,
    ) -> tuple[RuntimeEvent, ...]:
        native_item_id = _required_string(params.get("itemId"), "params.itemId")
        state = self._require_active_item(scope_id, native_item_id)
        source = _source(method, cursor, state)
        op, update = _item_update(method, params, state)
        return (
            ItemUpdated(
                **env(scope_id, state.item_id, "item.updated", update.part_id, source),
                item_id=state.item_id,
                item_kind=state.item_kind,
                op=op,
                update=update,
            ),
        )

    def _map_item_terminal(
        self,
        method: str,
        params: Mapping[str, Any],
        env: Callable[..., dict[str, Any]],
        *,
        scope_id: str,
        cursor: str,
    ) -> tuple[RuntimeEvent, ...]:
        item = _mapping(params.get("item"), "params.item")
        native_item_id = _required_string(item.get("id"), "params.itemId")
        state = self._require_active_item(scope_id, native_item_id)
        source = _source(method, cursor, state)
        native_kind = _required_string(item.get("type"), "params.item.type")
        if native_kind != state.native_item_kind:
            _fail(
                "conflicting_item_kind",
                "params.item.type",
                "Codex item changed type during its lifecycle",
            )
        snapshot = _completed_snapshot(state, item)
        del self._items[(scope_id, native_item_id)]
        if _item_failed(state, item):
            correction = snapshot.parts[-1]
            corrected = ItemUpdated(
                **env(scope_id, state.item_id, "item.updated", correction.part_id, source),
                item_id=state.item_id,
                item_kind=state.item_kind,
                op="replace",
                update=correction,
            )
            failed = ItemFailed(
                **env(scope_id, state.item_id, "item.failed", "failure", source),
                item_id=state.item_id,
                item_kind=state.item_kind,
                error=ErrorInfo(
                    code=f"codex_{_FAILURE_CODE_KINDS.get(state.native_item_kind, 'item')}_failed",
                    message=f"Codex {state.native_item_kind} failed",
                    source="codex",
                    scope_id=scope_id,
                    item_id=state.item_id,
                    source_ref=source,
                ),
            )
            return (corrected, failed)
        if state.phase == "final_answer":
            self._completed_items.setdefault(scope_id, []).append(
                OutputRef(scope_id=scope_id, item_id=state.item_id)
            )
        return (
            ItemCompleted(
                **env(scope_id, state.item_id, "item.completed", "snapshot", source),
                item_id=state.item_id,
                item_kind=state.item_kind,
                snapshot=snapshot,
            ),
        )

    def _require_active_item(self, scope_id: str, native_item_id: str) -> _ItemState:
        state = self._items.get((scope_id, native_item_id))
        if state is None:
            _fail(
                "item_not_started",
                "params.itemId",
                f"Codex item {native_item_id!r} mutated before item/started",
            )
        return state
