"""Codex app-server 0.144.4 JSONL messages to RuntimeEvent schema v2."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import JsonValue

from ksadk.events.canonical import (
    ApprovalRequest,
    ApprovalResponse,
    ContinuationCreated,
    ContinuationResumed,
    ErrorInfo,
    EventPhase,
    InteractionRequest,
    InteractionRequested,
    InteractionResolved,
    InteractionResponse,
    ItemCompleted,
    ItemFailed,
    ItemKind,
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
    StructuredInputResponse,
)
from ksadk.events.content import (
    ArtifactContent,
    ContentSnapshot,
    ContentValue,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.identity import stable_event_id, stable_item_id, stable_part_id, stable_scope_id

# Exact `notification_registry.NOTIFICATION_MODELS` keys from openai-codex 0.144.4.
# Keeping this source contract explicit makes a dependency upgrade fail its conformance test.
_CODEX_0_144_4_NOTIFICATION_METHODS = frozenset(
    {
        "account/login/completed",
        "account/rateLimits/updated",
        "account/updated",
        "app/list/updated",
        "command/exec/outputDelta",
        "configWarning",
        "deprecationNotice",
        "error",
        "externalAgentConfig/import/completed",
        "externalAgentConfig/import/progress",
        "fs/changed",
        "fuzzyFileSearch/sessionCompleted",
        "fuzzyFileSearch/sessionUpdated",
        "guardianWarning",
        "hook/completed",
        "hook/started",
        "item/agentMessage/delta",
        "item/autoApprovalReview/completed",
        "item/autoApprovalReview/started",
        "item/commandExecution/outputDelta",
        "item/commandExecution/terminalInteraction",
        "item/completed",
        "item/fileChange/outputDelta",
        "item/fileChange/patchUpdated",
        "item/mcpToolCall/progress",
        "item/plan/delta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "item/started",
        "mcpServer/oauthLogin/completed",
        "mcpServer/startupStatus/updated",
        "model/rerouted",
        "model/safetyBuffering/updated",
        "model/verification",
        "process/exited",
        "process/outputDelta",
        "remoteControl/status/changed",
        "serverRequest/resolved",
        "skills/changed",
        "thread/archived",
        "thread/closed",
        "thread/compacted",
        "thread/deleted",
        "thread/goal/cleared",
        "thread/goal/updated",
        "thread/name/updated",
        "thread/realtime/closed",
        "thread/realtime/error",
        "thread/realtime/itemAdded",
        "thread/realtime/outputAudio/delta",
        "thread/realtime/sdp",
        "thread/realtime/started",
        "thread/realtime/transcript/delta",
        "thread/realtime/transcript/done",
        "thread/settings/updated",
        "thread/started",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "thread/unarchived",
        "turn/completed",
        "turn/diff/updated",
        "turn/moderationMetadata",
        "turn/plan/updated",
        "turn/started",
        "warning",
        "windows/worldWritableWarning",
        "windowsSandbox/setupCompleted",
    }
)

_CODEX_0_144_4_DATA_ITEM_KINDS = frozenset(
    {
        "userMessage",
        "hookPrompt",
        "subAgentActivity",
        "imageView",
        "sleep",
        "enteredReviewMode",
        "exitedReviewMode",
        "contextCompaction",
    }
)

_CODEX_ERROR_INFO_VALUES = frozenset(
    {
        "contextWindowExceeded",
        "sessionBudgetExceeded",
        "usageLimitExceeded",
        "serverOverloaded",
        "cyberPolicy",
        "internalServerError",
        "unauthorized",
        "badRequest",
        "threadRollbackFailed",
        "sandboxError",
        "other",
    }
)
_CODEX_ERROR_INFO_VARIANTS = frozenset(
    {
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
        "activeTurnNotSteerable",
    }
)


class CodexMappingError(ValueError):
    """A Codex app-server message violates the locked native contract."""

    def __init__(self, code: str, field_name: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field_name = field_name
        self.source = "codex"


@dataclass
class CodexAdapterContext:
    """Runtime identity and deterministic pre-store placeholder ordering."""

    run_id: str
    initial_seq: int = 0
    _next_seq: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.run_id = _required_string(self.run_id, "runtime run_id")
        if self.initial_seq < 0:
            raise ValueError("Codex initial_seq must be non-negative")
        self._next_seq = self.initial_seq

    def allocate_placeholder_seq(self) -> int:
        value = self._next_seq
        self._next_seq += 1
        return value


@dataclass
class _ItemState:
    scope_id: str
    thread_id: str
    turn_id: str
    native_item_id: str
    native_item_kind: str
    item_id: str
    item_kind: ItemKind
    phase: EventPhase
    part_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _InteractionState:
    request_id: str
    interaction_id: str
    interaction_kind: Literal["approval", "structured_input"]
    scope_id: str
    thread_id: str
    turn_id: str
    native_item_id: str
    method: str
    interrupts_run: bool
    question_ids: frozenset[str] = frozenset()
    secret_question_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _ReplayRecord:
    payload_digest: str
    event_ids: tuple[str, ...]


class CodexEventAdapter:
    """Map one source-owned Codex JSONL frame at a time."""

    _REPLAY_WINDOW_LIMIT = 1024

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], _ItemState] = {}
        self._active_turns: set[str] = set()
        self._completed_items: dict[str, list[OutputRef]] = {}
        self._interactions: dict[str, _InteractionState] = {}
        self._thread_continuations: dict[str, str] = {}
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
                raise CodexMappingError(
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
                raise CodexMappingError(
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

        interaction_methods = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/call",
            "item/tool/requestUserInput",
            "mcpServer/elicitation/request",
        }
        control_interaction_methods = {
            "account/chatgptAuthTokens/refresh",
            "attestation/generate",
        }
        if method == "serverRequest/resolved":
            return self._map_server_request_resolved(
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        if method in control_interaction_methods:
            return self._map_control_interaction_request(
                message=message,
                method=method,
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        directly_typed_notifications = {
            "error",
            "item/started",
            "item/completed",
            "item/agentMessage/delta",
            "item/reasoning/textDelta",
            "item/reasoning/summaryPartAdded",
            "item/reasoning/summaryTextDelta",
            "item/commandExecution/outputDelta",
            "item/mcpToolCall/progress",
            "item/fileChange/patchUpdated",
            "item/fileChange/outputDelta",
            "item/plan/delta",
        }
        if method in _CODEX_0_144_4_NOTIFICATION_METHODS - directly_typed_notifications:
            return self._map_known_notification(
                method=method,
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        if method not in _CODEX_0_144_4_NOTIFICATION_METHODS | interaction_methods:
            raise CodexMappingError(
                "unsupported_method",
                "method",
                f"Unsupported Codex app-server method: {method}",
            )

        thread_id = _required_string(params.get("threadId"), "params.threadId")
        turn_value = params.get("turnId")
        interrupts_run = not (method == "mcpServer/elicitation/request" and turn_value is None)
        turn_id = (
            _required_string(turn_value, "params.turnId") if interrupts_run else "mcp_elicitation"
        )
        scope_id = stable_scope_id("codex", thread_id, turn_id)

        if method in interaction_methods:
            return self._map_interaction_request(
                message=message,
                method=method,
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
                thread_id=thread_id,
                turn_id=turn_id,
                scope_id=scope_id,
                interrupts_run=interrupts_run,
            )

        supported_item_methods = {
            "error",
            "item/started",
            "item/completed",
            "item/agentMessage/delta",
            "item/reasoning/textDelta",
            "item/reasoning/summaryPartAdded",
            "item/reasoning/summaryTextDelta",
            "item/commandExecution/outputDelta",
            "item/mcpToolCall/progress",
            "item/fileChange/patchUpdated",
            "item/fileChange/outputDelta",
            "item/plan/delta",
        }
        if method not in supported_item_methods:
            if method in _CODEX_0_144_4_NOTIFICATION_METHODS:
                return self._map_known_notification(
                    method=method,
                    params=params,
                    context=context,
                    cursor=cursor,
                    timestamp=timestamp,
                )
            raise CodexMappingError(
                "unsupported_method",
                "method",
                f"Unsupported Codex app-server method: {method}",
            )

        if method == "error":
            error = _mapping(params.get("error"), "params.error")
            _required_text(error.get("message"), "params.error.message")
            source = _protocol_source(
                method=method,
                cursor=cursor,
                thread_id=thread_id,
                turn_id=turn_id,
                native_item_id=None,
            )
            will_retry = params.get("willRetry")
            if not isinstance(will_retry, bool):
                raise CodexMappingError(
                    "invalid_protocol_message",
                    "params.willRetry",
                    "Codex params.willRetry must be a boolean",
                )
            source = source.model_copy(
                update={
                    "metadata": {
                        **source.metadata,
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
                    **_generic_envelope(
                        context=context,
                        scope_id=scope_id,
                        identity=turn_id,
                        event_type="run.progress",
                        part_id="retryable_error" if will_retry else "error_diagnostic",
                        cursor=cursor,
                        timestamp=timestamp,
                        source=source,
                    ),
                    status="running",
                    message=(
                        "Codex reported a retryable turn error"
                        if will_retry
                        else "Codex reported a non-retryable turn error"
                    ),
                ),
            )

        if method == "item/started":
            item = _mapping(params.get("item"), "params.item")
            native_item_id = _required_string(item.get("id"), "params.item.id")
            native_kind = _required_string(item.get("type"), "params.item.type")
            state = _item_state(scope_id, thread_id, turn_id, native_item_id, native_kind, item)
            key = (scope_id, native_item_id)
            if key in self._items:
                raise CodexMappingError(
                    "item_already_started",
                    "params.item.id",
                    f"Codex item {native_item_id!r} started twice",
                )
            self._items[key] = state
            source = _source(method, cursor, state)
            return (
                ItemStarted(
                    **_envelope(
                        context,
                        state,
                        "item.started",
                        "item",
                        cursor,
                        timestamp,
                        source,
                    ),
                    item_id=state.item_id,
                    item_kind=state.item_kind,
                    phase=state.phase,
                    initial=_initial_snapshot(state, item),
                ),
            )

        native_item_id = _required_string(
            params.get("itemId")
            if method != "item/completed"
            else _mapping(params.get("item"), "params.item").get("id"),
            "params.itemId",
        )
        key = (scope_id, native_item_id)
        active_item = self._items.get(key)
        if active_item is None:
            raise CodexMappingError(
                "item_not_started",
                "params.itemId",
                f"Codex item {native_item_id!r} mutated before item/started",
            )
        source = _source(method, cursor, active_item)

        if method != "item/completed":
            op, update = _item_update(method, params, active_item)
            return (
                ItemUpdated(
                    **_envelope(
                        context,
                        active_item,
                        "item.updated",
                        update.part_id,
                        cursor,
                        timestamp,
                        source,
                    ),
                    item_id=active_item.item_id,
                    item_kind=active_item.item_kind,
                    op=op,
                    update=update,
                ),
            )

        if method == "item/completed":
            item = _mapping(params.get("item"), "params.item")
            native_kind = _required_string(item.get("type"), "params.item.type")
            if native_kind != active_item.native_item_kind:
                raise CodexMappingError(
                    "conflicting_item_kind",
                    "params.item.type",
                    "Codex item changed type during its lifecycle",
                )
            snapshot = _completed_snapshot(active_item, item)
            del self._items[key]
            if _item_failed(active_item, item):
                correction = snapshot.parts[-1]
                corrected = ItemUpdated(
                    **_envelope(
                        context,
                        active_item,
                        "item.updated",
                        correction.part_id,
                        cursor,
                        timestamp,
                        source,
                    ),
                    item_id=active_item.item_id,
                    item_kind=active_item.item_kind,
                    op="replace",
                    update=correction,
                )
                failed = ItemFailed(
                    **_envelope(
                        context,
                        active_item,
                        "item.failed",
                        "failure",
                        cursor,
                        timestamp,
                        source,
                    ),
                    item_id=active_item.item_id,
                    item_kind=active_item.item_kind,
                    error=ErrorInfo(
                        code=f"codex_{_failure_code_kind(active_item.native_item_kind)}_failed",
                        message=f"Codex {active_item.native_item_kind} failed",
                        source="codex",
                        scope_id=scope_id,
                        item_id=active_item.item_id,
                        source_ref=source,
                    ),
                )
                return (corrected, failed)
            if active_item.phase == "final_answer":
                self._completed_items.setdefault(scope_id, []).append(
                    OutputRef(scope_id=scope_id, item_id=active_item.item_id)
                )
            return (
                ItemCompleted(
                    **_envelope(
                        context,
                        active_item,
                        "item.completed",
                        "snapshot",
                        cursor,
                        timestamp,
                        source,
                    ),
                    item_id=active_item.item_id,
                    item_kind=active_item.item_kind,
                    snapshot=snapshot,
                ),
            )

        raise CodexMappingError(
            "unsupported_method", "method", f"Unsupported Codex app-server method: {method}"
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
        raise CodexMappingError(
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
        native_item_id = f"{method}:{cursor}"
        state = _ItemState(
            scope_id=scope_id,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=native_item_id,
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
        return (
            ItemStarted(
                **_envelope(
                    context,
                    state,
                    "item.started",
                    "notification",
                    cursor,
                    timestamp,
                    source,
                ),
                item_id=state.item_id,
                item_kind="data",
                phase="commentary",
                initial=None,
            ),
            ItemCompleted(
                **_envelope(
                    context,
                    state,
                    "item.completed",
                    "snapshot",
                    cursor,
                    timestamp,
                    source,
                ),
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

        if method == "turn/started":
            if status != "inProgress":
                raise CodexMappingError(
                    "invalid_turn_status",
                    "params.turn.status",
                    f"Codex turn/started requires inProgress, got: {status}",
                )
            if scope_id in self._active_turns:
                raise CodexMappingError(
                    "turn_already_started",
                    "params.turn.id",
                    f"Codex turn {turn_id!r} started twice",
                )
            self._active_turns.add(scope_id)
            run_started = RunStarted(
                **_generic_envelope(
                    context=context,
                    scope_id=scope_id,
                    identity=turn_id,
                    event_type="run.started",
                    part_id="run",
                    cursor=cursor,
                    timestamp=timestamp,
                    source=source,
                ),
                status="running",
            )
            continuation_scope_id, derived_continuation_id = _thread_continuation_identity(
                thread_id
            )
            continuation_existed = thread_id in self._thread_continuations
            continuation_id = self._thread_continuations.setdefault(
                thread_id,
                derived_continuation_id,
            )
            resume_attempt = self._pending_resume_by_thread.pop(thread_id, None)
            if resume_attempt is not None:
                self._resume_requests.pop(resume_attempt, None)
                continuation: RuntimeEvent = ContinuationResumed(
                    **_generic_envelope(
                        context=context,
                        scope_id=continuation_scope_id,
                        identity=continuation_id,
                        event_type="continuation.resumed",
                        part_id="thread_resume",
                        cursor=cursor,
                        timestamp=timestamp,
                        source=source,
                    ),
                    continuation_id=continuation_id,
                    continuation_kind="thread_resume",
                    resume_attempt_id=resume_attempt,
                )
            elif not continuation_existed:
                continuation = ContinuationCreated(
                    **_generic_envelope(
                        context=context,
                        scope_id=continuation_scope_id,
                        identity=continuation_id,
                        event_type="continuation.created",
                        part_id="thread_resume",
                        cursor=cursor,
                        timestamp=timestamp,
                        source=source,
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
                return (run_started,)
            self._completed_items.setdefault(scope_id, [])
            return (run_started, continuation)

        if scope_id not in self._active_turns:
            raise CodexMappingError(
                "turn_not_started",
                "params.turn.id",
                f"Codex turn {turn_id!r} completed before turn/started",
            )
        open_items = sorted(
            state.native_item_id for state in self._items.values() if state.scope_id == scope_id
        )
        if open_items:
            raise CodexMappingError(
                "open_items_at_turn_end",
                "item/completed",
                f"Codex turn ended with open items: {open_items}",
            )
        output_refs = tuple(self._completed_items.get(scope_id, ()))
        items = turn.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise CodexMappingError(
                "invalid_turn_snapshot",
                "params.turn.items",
                "Codex turn items must be an array",
            )

        if status == "completed":
            terminal: RuntimeEvent = RunCompleted(
                **_generic_envelope(
                    context=context,
                    scope_id=scope_id,
                    identity=turn_id,
                    event_type="run.completed",
                    part_id="run",
                    cursor=cursor,
                    timestamp=timestamp,
                    source=source,
                ),
                status="completed",
                output_refs=output_refs,
            )
        elif status == "failed":
            error = _mapping(turn.get("error"), "params.turn.error")
            message = _required_text(error.get("message"), "params.turn.error.message")
            terminal = RunFailed(
                **_generic_envelope(
                    context=context,
                    scope_id=scope_id,
                    identity=turn_id,
                    event_type="run.failed",
                    part_id="run",
                    cursor=cursor,
                    timestamp=timestamp,
                    source=source,
                ),
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
                **_generic_envelope(
                    context=context,
                    scope_id=scope_id,
                    identity=turn_id,
                    event_type="run.canceled",
                    part_id="run",
                    cursor=cursor,
                    timestamp=timestamp,
                    source=source,
                ),
                status="canceled",
                reason="Codex turn/interrupt completed",
            )
        else:
            raise CodexMappingError(
                "invalid_turn_status",
                "params.turn.status",
                f"Unsupported terminal Codex turn status: {status}",
            )
        self._active_turns.remove(scope_id)
        self._completed_items.pop(scope_id, None)
        return (terminal,)

    def _map_control_interaction_request(
        self,
        *,
        message: Mapping[str, Any],
        method: str,
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        """Map process-level v2 requests without inventing a turn interruption."""

        request_id = _request_id(message.get("id"), "id")
        if request_id in self._interactions:
            raise CodexMappingError(
                "interaction_already_pending",
                "id",
                f"Codex JSON-RPC request {request_id!r} is already pending",
            )
        thread_id = f"runtime:{context.run_id}"
        turn_id = "control"
        scope_id = stable_scope_id("codex", thread_id, turn_id)
        interaction_id = stable_item_id("codex", scope_id, "interaction", method, request_id)
        if method == "account/chatgptAuthTokens/refresh":
            reason = _required_string(params.get("reason"), "params.reason")
            if reason != "unauthorized":
                raise CodexMappingError(
                    "invalid_interaction_request",
                    "params.reason",
                    f"Unsupported ChatGPT token refresh reason: {reason}",
                )
            previous_account_id = params.get("previousAccountId")
            if previous_account_id is not None:
                _required_string(previous_account_id, "params.previousAccountId")
            request = StructuredInputRequest(
                prompt="Refresh ChatGPT authentication tokens",
                schema={
                    "type": "object",
                    "properties": {
                        "accessToken": {"type": "string"},
                        "chatgptAccountId": {"type": "string"},
                        "chatgptPlanType": {"type": ["string", "null"]},
                    },
                    "required": ["accessToken", "chatgptAccountId"],
                    "x-codex-request": _json_value(params),
                },
            )
        elif method == "attestation/generate":
            if params:
                raise CodexMappingError(
                    "invalid_interaction_request",
                    "params",
                    "Codex attestation/generate params must be empty",
                )
            request = StructuredInputRequest(
                prompt="Generate an upstream attestation token",
                schema={
                    "type": "object",
                    "properties": {"token": {"type": "string"}},
                    "required": ["token"],
                },
            )
        else:  # pragma: no cover - caller exhaustively validates method
            raise AssertionError("validated Codex control interaction method")
        state = _InteractionState(
            request_id=request_id,
            interaction_id=interaction_id,
            interaction_kind="structured_input",
            scope_id=scope_id,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=request_id,
            method=method,
            interrupts_run=False,
        )
        self._interactions[request_id] = state
        source = _protocol_source(
            method=method,
            cursor=cursor,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=request_id,
            native_event_id=request_id,
        )
        return (
            InteractionRequested(
                **_generic_envelope(
                    context=context,
                    scope_id=scope_id,
                    identity=interaction_id,
                    event_type="interaction.requested",
                    part_id="structured_input",
                    cursor=cursor,
                    timestamp=timestamp,
                    source=source,
                ),
                interaction_id=interaction_id,
                interaction_kind="structured_input",
                request=request,
            ),
        )

    def _map_server_request_resolved(
        self,
        *,
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        """Close a request that Codex resolved outside its JSON-RPC response path."""

        thread_id = _required_string(params.get("threadId"), "params.threadId")
        request_id = _request_id(params.get("requestId"), "params.requestId")
        state = self._interactions.get(request_id)
        if state is None:
            return self._map_known_notification(
                method="serverRequest/resolved",
                params=params,
                context=context,
                cursor=cursor,
                timestamp=timestamp,
            )
        if state.thread_id != thread_id:
            raise CodexMappingError(
                "interaction_scope_mismatch",
                "params.threadId",
                "Codex serverRequest/resolved threadId does not match the pending request",
            )
        response: InteractionResponse
        if state.interaction_kind == "approval":
            response = ApprovalResponse(
                decision="canceled",
                data={"source": "serverRequest/resolved", "requestId": request_id},
            )
        else:
            response = StructuredInputResponse(
                data={"source": "serverRequest/resolved", "requestId": request_id}
            )
        source = _protocol_source(
            method="serverRequest/resolved",
            cursor=cursor,
            thread_id=state.thread_id,
            turn_id=state.turn_id,
            native_item_id=state.native_item_id,
            native_event_id=request_id,
        )
        resolved = InteractionResolved(
            **_generic_envelope(
                context=context,
                scope_id=state.scope_id,
                identity=state.interaction_id,
                event_type="interaction.resolved",
                part_id=state.interaction_kind,
                cursor=cursor,
                timestamp=timestamp,
                source=source,
            ),
            interaction_id=state.interaction_id,
            interaction_kind=state.interaction_kind,
            response=response,
        )
        del self._interactions[request_id]
        return (resolved,)

    def _map_interaction_request(
        self,
        *,
        message: Mapping[str, Any],
        method: str,
        params: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
        thread_id: str,
        turn_id: str,
        scope_id: str,
        interrupts_run: bool,
    ) -> tuple[RuntimeEvent, ...]:
        request_id = _request_id(message.get("id"), "id")
        if request_id in self._interactions:
            raise CodexMappingError(
                "interaction_already_pending",
                "id",
                f"Codex JSON-RPC request {request_id!r} is already pending",
            )
        if method == "item/tool/call":
            native_item_id = _required_string(params.get("callId"), "params.callId")
        elif method == "mcpServer/elicitation/request":
            native_item_id = request_id
        else:
            native_item_id = _required_string(params.get("itemId"), "params.itemId")
        native_interaction_id = params.get("approvalId") or request_id
        native_interaction_id = _required_string(native_interaction_id, "params.approvalId")
        interaction_id = stable_item_id(
            "codex", scope_id, "interaction", method, native_interaction_id
        )
        kind: Literal["approval", "structured_input"]
        request: InteractionRequest
        question_ids: frozenset[str] = frozenset()
        secret_question_ids: frozenset[str] = frozenset()
        if method == "item/tool/requestUserInput":
            kind = "structured_input"
            prompt, schema, question_ids, secret_question_ids = _question_schema(
                params.get("questions")
            )
            request = StructuredInputRequest(prompt=prompt, schema=schema)
        elif method == "mcpServer/elicitation/request":
            kind = "structured_input"
            mode = _required_string(params.get("mode"), "params.mode")
            prompt = _required_text(params.get("message"), "params.message")
            if mode in {"form", "openai/form"}:
                schema_value = _mapping(params.get("requestedSchema"), "params.requestedSchema")
                schema = cast(dict[str, JsonValue], _json_value(schema_value))
            elif mode == "url":
                schema = {
                    "type": "object",
                    "x-codex-elicitation-url": _required_text(params.get("url"), "params.url"),
                    "x-codex-elicitation-id": _required_text(
                        params.get("elicitationId"), "params.elicitationId"
                    ),
                }
            else:
                raise CodexMappingError(
                    "invalid_interaction_request",
                    "params.mode",
                    f"Unsupported MCP elicitation mode: {mode}",
                )
            request = StructuredInputRequest(prompt=prompt, schema=schema)
        else:
            kind = "approval"
            approval_kind = {
                "item/commandExecution/requestApproval": "command_execution",
                "item/fileChange/requestApproval": "file_change",
                "item/permissions/requestApproval": "permissions",
                "item/tool/call": "dynamic_tool_call",
            }[method]
            request = ApprovalRequest(
                call_id=native_item_id,
                kind=approval_kind,
                detail=_json_value(params),
            )
        state = _InteractionState(
            request_id=request_id,
            interaction_id=interaction_id,
            interaction_kind=kind,
            scope_id=scope_id,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=native_item_id,
            method=method,
            interrupts_run=interrupts_run,
            question_ids=question_ids,
            secret_question_ids=secret_question_ids,
        )
        self._interactions[request_id] = state
        source = _protocol_source(
            method=method,
            cursor=cursor,
            thread_id=thread_id,
            turn_id=turn_id,
            native_item_id=native_item_id,
            native_event_id=request_id,
        )
        requested = InteractionRequested(
            **_generic_envelope(
                context=context,
                scope_id=scope_id,
                identity=interaction_id,
                event_type="interaction.requested",
                part_id=kind,
                cursor=cursor,
                timestamp=timestamp,
                source=source,
            ),
            interaction_id=interaction_id,
            interaction_kind=kind,
            request=request,
        )
        interrupted = RunInterrupted(
            **_generic_envelope(
                context=context,
                scope_id=scope_id,
                identity=turn_id,
                event_type="run.interrupted",
                part_id=interaction_id,
                cursor=cursor,
                timestamp=timestamp,
                source=source,
            ),
            status="interrupted",
            reason="Codex requires user interaction",
            interaction_id=interaction_id,
            continuation_id=self._thread_continuations.setdefault(
                thread_id, _thread_continuation_identity(thread_id)[1]
            ),
        )
        return (requested, interrupted) if interrupts_run else (requested,)

    def _map_jsonrpc_response(
        self,
        message: Mapping[str, Any],
        context: CodexAdapterContext,
        cursor: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        request_id = _request_id(message.get("id"), "id")
        if request_id in self._resume_requests:
            if "error" in message:
                thread_id = self._resume_requests.pop(request_id)
                self._pending_resume_by_thread.pop(thread_id, None)
                raise CodexMappingError(
                    "thread_resume_failed",
                    "error",
                    "Codex thread/resume failed with a JSON-RPC error",
                )
            _mapping(message.get("result"), "result")
            return ()
        state = self._interactions.get(request_id)
        if state is None:
            raise CodexMappingError(
                "unknown_jsonrpc_response",
                "id",
                f"Codex response has no pending request: {request_id}",
            )
        response: InteractionResponse
        is_error_response = "error" in message
        if is_error_response:
            error = _mapping(message.get("error"), "error")
            code = error.get("code")
            if isinstance(code, bool) or not isinstance(code, int):
                raise CodexMappingError(
                    "invalid_interaction_response",
                    "error.code",
                    "Codex JSON-RPC error.code must be an integer",
                )
            _required_text(error.get("message"), "error.message")
            sanitized_error: dict[str, JsonValue] = {
                "code": code,
                "messagePresent": True,
                "dataPresent": "data" in error,
            }
            if state.interaction_kind == "approval":
                response = ApprovalResponse(
                    decision="canceled",
                    data={"jsonrpcError": sanitized_error},
                )
            else:
                response = StructuredInputResponse(data={"jsonrpcError": sanitized_error})
            result: Mapping[str, Any] = {}
        else:
            result = _mapping(message.get("result"), "result")
        if not is_error_response and state.interaction_kind == "approval":
            if state.method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                if result.get("decision") is None:
                    raise CodexMappingError(
                        "missing_native_identity",
                        "result.decision",
                        "Codex approval result.decision is required",
                    )
                response = ApprovalResponse(
                    decision=_approval_decision(result.get("decision")),
                    data=_json_value(result),
                )
            elif state.method == "item/permissions/requestApproval":
                response = ApprovalResponse(decision="approved", data=_json_value(result))
            elif state.method == "item/tool/call":
                success = result.get("success")
                if not isinstance(success, bool):
                    raise CodexMappingError(
                        "invalid_interaction_response",
                        "result.success",
                        "Codex dynamic tool result.success must be a boolean",
                    )
                response = ApprovalResponse(
                    decision="approved" if success else "rejected",
                    data=_json_value(result),
                )
            else:  # pragma: no cover - state creation exhaustively validates method
                raise AssertionError("validated Codex approval interaction method")
        elif not is_error_response:
            if state.method == "account/chatgptAuthTokens/refresh":
                _required_string(result.get("accessToken"), "result.accessToken")
                account_id = _required_string(
                    result.get("chatgptAccountId"), "result.chatgptAccountId"
                )
                plan_type = result.get("chatgptPlanType")
                if plan_type is not None:
                    _required_string(plan_type, "result.chatgptPlanType")
                response_data: JsonValue = {
                    "accessTokenPresent": True,
                    "chatgptAccountId": account_id,
                    "chatgptPlanType": cast(JsonValue, plan_type),
                }
            elif state.method == "attestation/generate":
                _required_string(result.get("token"), "result.token")
                response_data = {"tokenPresent": True}
            elif state.method == "item/tool/requestUserInput":
                response_data = _validated_user_input_answers(
                    result,
                    state.question_ids,
                    state.secret_question_ids,
                )
            else:
                response_data = _json_value(result)
            response = StructuredInputResponse(data=response_data)
        del self._interactions[request_id]
        source = _protocol_source(
            method="jsonrpc/response",
            cursor=cursor,
            thread_id=state.thread_id,
            turn_id=state.turn_id,
            native_item_id=state.native_item_id,
            native_event_id=request_id,
        )
        resolved = InteractionResolved(
            **_generic_envelope(
                context=context,
                scope_id=state.scope_id,
                identity=state.interaction_id,
                event_type="interaction.resolved",
                part_id=state.interaction_kind,
                cursor=cursor,
                timestamp=timestamp,
                source=source,
            ),
            interaction_id=state.interaction_id,
            interaction_kind=state.interaction_kind,
            response=response,
        )
        resumes = not is_error_response and (
            (
                isinstance(response, ApprovalResponse)
                and response.decision in {"approved", "rejected"}
            )
            or (
                isinstance(response, StructuredInputResponse)
                and (
                    state.method == "item/tool/requestUserInput"
                    or result.get("action") in {"accept", "decline"}
                )
            )
        )
        if not resumes or not state.interrupts_run:
            return (resolved,)
        return (
            resolved,
            RunProgress(
                **_generic_envelope(
                    context=context,
                    scope_id=state.scope_id,
                    identity=state.turn_id,
                    event_type="run.progress",
                    part_id=state.interaction_id,
                    cursor=cursor,
                    timestamp=timestamp,
                    source=source,
                ),
                status="running",
                message="Codex user interaction resolved; turn resumed",
            ),
        )


def _item_state(
    scope_id: str,
    thread_id: str,
    turn_id: str,
    native_item_id: str,
    native_kind: str,
    item: Mapping[str, Any],
) -> _ItemState:
    item_id = stable_item_id("codex", scope_id, native_kind, native_item_id)
    item_kind: ItemKind
    phase: EventPhase
    if native_kind == "agentMessage":
        item_kind = "message"
        phase_value = item.get("phase")
        if phase_value in {None, "final_answer"}:
            phase = "final_answer"
        elif phase_value == "commentary":
            phase = "commentary"
        else:
            raise CodexMappingError(
                "invalid_item_phase",
                "params.item.phase",
                f"Unsupported Codex phase: {phase_value}",
            )
    elif native_kind == "reasoning":
        item_kind = "reasoning"
        phase = "commentary"
    elif native_kind in {
        "commandExecution",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "webSearch",
    }:
        item_kind = "tool_call"
        phase = "commentary"
    elif native_kind in {"fileChange", "plan"} | _CODEX_0_144_4_DATA_ITEM_KINDS:
        item_kind = "data"
        phase = "commentary"
    elif native_kind == "imageGeneration":
        item_kind = "artifact"
        phase = "commentary"
    else:
        raise CodexMappingError(
            "unsupported_item_kind",
            "params.item.type",
            f"Unsupported Codex item type: {native_kind}",
        )
    return _ItemState(
        scope_id=scope_id,
        thread_id=thread_id,
        turn_id=turn_id,
        native_item_id=native_item_id,
        native_item_kind=native_kind,
        item_id=item_id,
        item_kind=item_kind,
        phase=phase,
    )


def _part_id(state: _ItemState, native_part_kind: str, native_part_id: str) -> str:
    lane = f"{native_part_kind}:{native_part_id}"
    part_id = state.part_ids.get(lane)
    if part_id is None:
        part_id = stable_part_id("codex", state.item_id, native_part_kind, native_part_id)
        state.part_ids[lane] = part_id
    return part_id


def _initial_snapshot(state: _ItemState, item: Mapping[str, Any]) -> ContentSnapshot | None:
    if state.native_item_kind in {"agentMessage", "reasoning", "plan"}:
        return None
    if state.native_item_kind == "commandExecution":
        return ContentSnapshot(parts=(_command_call(state, item),))
    if state.native_item_kind == "mcpToolCall":
        return ContentSnapshot(parts=(_mcp_call(state, item),))
    if state.native_item_kind == "fileChange":
        return ContentSnapshot(parts=(_file_change(state, item),))
    if state.native_item_kind in {
        "dynamicToolCall",
        "collabAgentToolCall",
        "webSearch",
    }:
        return ContentSnapshot(parts=(_additional_tool_call(state, item),))
    if state.native_item_kind == "imageGeneration":
        return ContentSnapshot(parts=(_image_artifact(state, item),))
    if state.native_item_kind in _CODEX_0_144_4_DATA_ITEM_KINDS:
        return ContentSnapshot(parts=(_generic_item_data(state, item),))
    raise AssertionError("validated Codex item kind must have initial semantics")


def _item_update(
    method: str,
    params: Mapping[str, Any],
    state: _ItemState,
) -> tuple[Literal["append", "replace"], ContentValue]:
    if method == "item/agentMessage/delta" and state.native_item_kind == "agentMessage":
        return "append", TextContent(
            part_id=_part_id(state, "text", "primary"),
            text=_required_text(params.get("delta"), "params.delta"),
        )
    if method == "item/reasoning/textDelta" and state.native_item_kind == "reasoning":
        content_index = _nonnegative_int(params.get("contentIndex"), "params.contentIndex")
        return "append", TextContent(
            part_id=_part_id(state, "reasoning_content", str(content_index)),
            text=_required_text(params.get("delta"), "params.delta"),
        )
    if (
        method
        in {
            "item/reasoning/summaryPartAdded",
            "item/reasoning/summaryTextDelta",
        }
        and state.native_item_kind == "reasoning"
    ):
        summary_index = _nonnegative_int(params.get("summaryIndex"), "params.summaryIndex")
        delta = (
            ""
            if method == "item/reasoning/summaryPartAdded"
            else _required_text(params.get("delta"), "params.delta")
        )
        return (
            "replace" if method == "item/reasoning/summaryPartAdded" else "append"
        ), TextContent(
            part_id=_part_id(state, "reasoning_summary", str(summary_index)),
            text=delta,
        )
    if (
        method == "item/commandExecution/outputDelta"
        and state.native_item_kind == "commandExecution"
    ):
        return "append", TextContent(
            part_id=_part_id(state, "command_output", "primary"),
            text=_required_text(params.get("delta"), "params.delta"),
        )
    if method == "item/mcpToolCall/progress" and state.native_item_kind == "mcpToolCall":
        return "replace", TextContent(
            part_id=_part_id(state, "mcp_progress", "primary"),
            text=_required_text(params.get("message"), "params.message"),
        )
    if method == "item/fileChange/patchUpdated" and state.native_item_kind == "fileChange":
        changes = _json_value(params.get("changes"))
        if not isinstance(changes, list):
            raise CodexMappingError(
                "invalid_item_update",
                "params.changes",
                "Codex file changes must be an array",
            )
        return "replace", DataContent(
            part_id=_part_id(state, "file_changes", "primary"),
            data={"changes": changes, "status": "inProgress"},
        )
    if method == "item/fileChange/outputDelta" and state.native_item_kind == "fileChange":
        return "append", TextContent(
            part_id=_part_id(state, "file_output", "primary"),
            text=_required_text(params.get("delta"), "params.delta"),
        )
    if method == "item/plan/delta" and state.native_item_kind == "plan":
        return "append", TextContent(
            part_id=_part_id(state, "plan_text", "primary"),
            text=_required_text(params.get("delta"), "params.delta"),
        )
    raise CodexMappingError(
        "unsupported_item_mutation",
        "method",
        f"Codex method {method!r} does not match {state.native_item_kind!r}",
    )


def _completed_snapshot(state: _ItemState, item: Mapping[str, Any]) -> ContentSnapshot:
    if state.native_item_kind == "agentMessage":
        return ContentSnapshot(
            parts=(
                TextContent(
                    part_id=_part_id(state, "text", "primary"),
                    text=_required_text(item.get("text"), "params.item.text"),
                ),
            )
        )
    if state.native_item_kind == "reasoning":
        summary = _string_sequence(item.get("summary"), "params.item.summary")
        content = _string_sequence(item.get("content"), "params.item.content")
        parts = tuple(
            TextContent(part_id=_part_id(state, "reasoning_summary", str(index)), text=text)
            for index, text in enumerate(summary)
        ) + tuple(
            TextContent(part_id=_part_id(state, "reasoning_content", str(index)), text=text)
            for index, text in enumerate(content)
        )
        return ContentSnapshot(parts=parts)
    if state.native_item_kind == "commandExecution":
        return ContentSnapshot(parts=(_command_call(state, item), _command_result(state, item)))
    if state.native_item_kind == "mcpToolCall":
        return ContentSnapshot(parts=(_mcp_call(state, item), _mcp_result(state, item)))
    if state.native_item_kind == "fileChange":
        return ContentSnapshot(parts=(_file_change(state, item),))
    if state.native_item_kind == "plan":
        return ContentSnapshot(
            parts=(
                TextContent(
                    part_id=_part_id(state, "plan_text", "primary"),
                    text=_required_text(item.get("text"), "params.item.text"),
                ),
            )
        )
    if state.native_item_kind in {
        "dynamicToolCall",
        "collabAgentToolCall",
        "webSearch",
    }:
        return ContentSnapshot(
            parts=(
                _additional_tool_call(state, item),
                _additional_tool_result(state, item),
            )
        )
    if state.native_item_kind == "imageGeneration":
        return ContentSnapshot(parts=(_image_artifact(state, item),))
    if state.native_item_kind in _CODEX_0_144_4_DATA_ITEM_KINDS:
        return ContentSnapshot(parts=(_generic_item_data(state, item),))
    raise AssertionError("validated Codex item kind must have completion semantics")


def _command_call(state: _ItemState, item: Mapping[str, Any]) -> ToolCallContent:
    return ToolCallContent(
        part_id=_part_id(state, "command_call", "primary"),
        call_id=state.native_item_id,
        name="codex.command",
        arguments={
            "command": _required_text(item.get("command"), "params.item.command"),
            "cwd": _required_text(item.get("cwd"), "params.item.cwd"),
            "commandActions": _json_value(item.get("commandActions")),
        },
    )


def _command_result(state: _ItemState, item: Mapping[str, Any]) -> ToolResultContent:
    status = _required_string(item.get("status"), "params.item.status")
    if status not in {"completed", "failed", "declined"}:
        raise CodexMappingError(
            "invalid_item_snapshot",
            "params.item.status",
            f"Command completed with non-terminal status: {status}",
        )
    exit_code = item.get("exitCode")
    if exit_code is not None and not isinstance(exit_code, int):
        raise CodexMappingError(
            "invalid_item_snapshot", "params.item.exitCode", "Codex exitCode must be an integer"
        )
    return ToolResultContent(
        part_id=_part_id(state, "command_result", "primary"),
        call_id=state.native_item_id,
        result={
            "status": status,
            "exit_code": exit_code,
            "duration_ms": _optional_int(item.get("durationMs"), "params.item.durationMs"),
            "output": _required_text(
                item.get("aggregatedOutput") or "", "params.item.aggregatedOutput"
            ),
            "process_id": item.get("processId"),
            "source": item.get("source"),
        },
        is_error=status in {"failed", "declined"}
        or (isinstance(exit_code, int) and exit_code != 0),
    )


def _mcp_call(state: _ItemState, item: Mapping[str, Any]) -> ToolCallContent:
    server = _required_string(item.get("server"), "params.item.server")
    tool = _required_string(item.get("tool"), "params.item.tool")
    return ToolCallContent(
        part_id=_part_id(state, "mcp_call", "primary"),
        call_id=state.native_item_id,
        name=f"mcp.{server}.{tool}",
        arguments=_json_value(item.get("arguments")),
    )


def _mcp_result(state: _ItemState, item: Mapping[str, Any]) -> ToolResultContent:
    status = _required_string(item.get("status"), "params.item.status")
    if status not in {"completed", "failed"}:
        raise CodexMappingError(
            "invalid_item_snapshot",
            "params.item.status",
            f"MCP call completed with non-terminal status: {status}",
        )
    result = _json_value(item.get("result"))
    error = _json_value(item.get("error"))
    result_value: dict[str, JsonValue] = {"status": status}
    if isinstance(result, dict):
        result_value.update(result)
    elif result is not None:
        result_value["result"] = result
    result_value["duration_ms"] = _optional_int(item.get("durationMs"), "params.item.durationMs")
    if error is not None:
        result_value["error"] = error
    return ToolResultContent(
        part_id=_part_id(state, "mcp_result", "primary"),
        call_id=state.native_item_id,
        result=result_value,
        is_error=status == "failed",
    )


def _file_change(state: _ItemState, item: Mapping[str, Any]) -> DataContent:
    changes = _json_value(item.get("changes"))
    if not isinstance(changes, list):
        raise CodexMappingError(
            "invalid_item_snapshot",
            "params.item.changes",
            "Codex file changes must be an array",
        )
    status = _required_string(item.get("status"), "params.item.status")
    return DataContent(
        part_id=_part_id(state, "file_changes", "primary"),
        data={"changes": changes, "status": status},
    )


def _generic_item_data(state: _ItemState, item: Mapping[str, Any]) -> DataContent:
    return DataContent(
        part_id=_part_id(state, "native_item", "primary"),
        data=_json_value(item),
    )


def _additional_tool_call(state: _ItemState, item: Mapping[str, Any]) -> ToolCallContent:
    kind = state.native_item_kind
    if kind == "dynamicToolCall":
        name = _required_string(item.get("tool"), "params.item.tool")
        arguments: JsonValue = {
            "arguments": _json_value(item.get("arguments")),
            "namespace": _json_value(item.get("namespace")),
        }
    elif kind == "collabAgentToolCall":
        name = f"codex.collab.{_required_string(item.get('tool'), 'params.item.tool')}"
        arguments = cast(
            JsonValue,
            {
                "senderThreadId": _json_value(item.get("senderThreadId")),
                "receiverThreadIds": _json_value(item.get("receiverThreadIds")),
                "prompt": _json_value(item.get("prompt")),
                "model": _json_value(item.get("model")),
                "reasoningEffort": _json_value(item.get("reasoningEffort")),
            },
        )
    elif kind == "webSearch":
        name = "codex.web_search"
        arguments = {
            "query": _required_text(item.get("query"), "params.item.query"),
            "action": _json_value(item.get("action")),
        }
    else:  # pragma: no cover - caller exhaustively validates native kind
        raise AssertionError("validated additional Codex tool kind")
    return ToolCallContent(
        part_id=_part_id(state, "tool_call", "primary"),
        call_id=state.native_item_id,
        name=name,
        arguments=arguments,
    )


def _additional_tool_result(state: _ItemState, item: Mapping[str, Any]) -> ToolResultContent:
    kind = state.native_item_kind
    if kind == "dynamicToolCall":
        status = _required_string(item.get("status"), "params.item.status")
        if status not in {"completed", "failed"}:
            raise CodexMappingError(
                "invalid_item_snapshot",
                "params.item.status",
                f"Dynamic tool completed with non-terminal status: {status}",
            )
        result: JsonValue = {
            "status": status,
            "success": _json_value(item.get("success")),
            "contentItems": _json_value(item.get("contentItems")),
            "durationMs": _json_value(item.get("durationMs")),
        }
        is_error = status == "failed" or item.get("success") is False
    elif kind == "collabAgentToolCall":
        status = _required_string(item.get("status"), "params.item.status")
        if status not in {"completed", "failed"}:
            raise CodexMappingError(
                "invalid_item_snapshot",
                "params.item.status",
                f"Collab tool completed with non-terminal status: {status}",
            )
        result = {
            "status": status,
            "agentsStates": _json_value(item.get("agentsStates")),
        }
        is_error = status == "failed"
    elif kind == "webSearch":
        result = {"action": _json_value(item.get("action"))}
        is_error = False
    else:  # pragma: no cover - caller exhaustively validates native kind
        raise AssertionError("validated additional Codex tool kind")
    return ToolResultContent(
        part_id=_part_id(state, "tool_result", "primary"),
        call_id=state.native_item_id,
        result=result,
        is_error=is_error,
    )


def _image_artifact(state: _ItemState, item: Mapping[str, Any]) -> ArtifactContent:
    result = _required_text(item.get("result"), "params.item.result")
    saved_path = item.get("savedPath")
    if saved_path is not None and not isinstance(saved_path, str):
        raise CodexMappingError(
            "invalid_item_snapshot",
            "params.item.savedPath",
            "Codex image savedPath must be text",
        )
    return ArtifactContent(
        part_id=_part_id(state, "image", "primary"),
        artifact_id=state.native_item_id,
        name=(saved_path.rsplit("/", 1)[-1] if saved_path else state.native_item_id),
        uri=result or saved_path,
        data={
            "status": _required_string(item.get("status"), "params.item.status"),
            "revisedPrompt": _json_value(item.get("revisedPrompt")),
            "result": result,
            "savedPath": _json_value(saved_path),
        },
    )


def _item_failed(state: _ItemState, item: Mapping[str, Any]) -> bool:
    if state.native_item_kind == "commandExecution":
        return item.get("status") in {"failed", "declined"}
    if state.native_item_kind == "mcpToolCall":
        return item.get("status") == "failed"
    if state.native_item_kind == "fileChange":
        return item.get("status") in {"failed", "declined"}
    if state.native_item_kind in {"dynamicToolCall", "collabAgentToolCall"}:
        return item.get("status") == "failed"
    return False


def _failure_code_kind(native_item_kind: str) -> str:
    return {
        "commandExecution": "command",
        "mcpToolCall": "mcp_tool",
        "fileChange": "file_change",
    }.get(native_item_kind, "item")


def _source(method: str, cursor: str, state: _ItemState) -> SourceRef:
    return SourceRef(
        framework="codex",
        native_cursor=cursor,
        native_run_id=state.turn_id,
        native_item_id=state.native_item_id,
        metadata=cast(
            dict[str, JsonValue],
            {
                "app_server_version": "0.144.4",
                "method": method,
                "thread_id": state.thread_id,
                "turn_id": state.turn_id,
                "native_item_kind": state.native_item_kind,
                "cursor_semantics": "jsonl",
            },
        ),
    )


def _protocol_source(
    *,
    method: str,
    cursor: str,
    thread_id: str,
    turn_id: str,
    native_item_id: str | None,
    native_event_id: str | None = None,
) -> SourceRef:
    return SourceRef(
        framework="codex",
        native_event_id=native_event_id,
        native_cursor=cursor,
        native_run_id=turn_id,
        native_item_id=native_item_id,
        metadata=cast(
            dict[str, JsonValue],
            {
                "app_server_version": "0.144.4",
                "method": method,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "cursor_semantics": "jsonl",
            },
        ),
    )


def _thread_continuation_identity(thread_id: str) -> tuple[str, str]:
    scope_id = stable_scope_id("codex", thread_id, "thread_resume")
    return scope_id, stable_item_id("codex", scope_id, "thread_resume", thread_id)


def _generic_envelope(
    *,
    context: CodexAdapterContext,
    scope_id: str,
    identity: str,
    event_type: str,
    part_id: str,
    cursor: str,
    timestamp: float,
    source: SourceRef,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "event_id": stable_event_id("codex", scope_id, identity, event_type, part_id, cursor, 0),
        "seq": context.allocate_placeholder_seq(),
        "timestamp": timestamp,
        "run_id": context.run_id,
        "scope_id": scope_id,
        "source": source,
    }


def _envelope(
    context: CodexAdapterContext,
    state: _ItemState,
    event_type: str,
    part_id: str,
    cursor: str,
    timestamp: float,
    source: SourceRef,
) -> dict[str, Any]:
    return _generic_envelope(
        context=context,
        scope_id=state.scope_id,
        identity=state.item_id,
        event_type=event_type,
        part_id=part_id,
        cursor=cursor,
        timestamp=timestamp,
        source=source,
    )


def _request_id(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CodexMappingError(
            "missing_native_identity",
            field_name,
            "Codex JSON-RPC id must be a string or integer",
        )
    normalized = str(value)
    if not normalized:
        raise CodexMappingError(
            "missing_native_identity", field_name, "Codex JSON-RPC id cannot be empty"
        )
    return normalized


def _safe_codex_error_info_kind(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value in _CODEX_ERROR_INFO_VALUES else "unknown"
    if isinstance(value, Mapping):
        for variant in _CODEX_ERROR_INFO_VARIANTS:
            if variant in value:
                return variant
    return "unknown"


def _validated_user_input_answers(
    result: Mapping[str, Any],
    question_ids: frozenset[str],
    secret_question_ids: frozenset[str],
) -> dict[str, JsonValue]:
    answers = _mapping(result.get("answers"), "result.answers")
    sanitized: dict[str, JsonValue] = {}
    for raw_question_id, raw_answer in answers.items():
        question_id = _required_string(raw_question_id, "result.answers question id")
        if question_id not in question_ids:
            raise CodexMappingError(
                "invalid_interaction_response",
                "result.answers",
                "Codex requestUserInput response contains an unknown question id",
            )
        answer = _mapping(raw_answer, f"result.answers.{question_id}")
        values = answer.get("answers")
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes))
            or any(not isinstance(value, str) for value in values)
        ):
            raise CodexMappingError(
                "invalid_interaction_response",
                f"result.answers.{question_id}.answers",
                "Codex requestUserInput answers must be a string array",
            )
        if question_id in secret_question_ids:
            sanitized[question_id] = {"answersPresent": True, "redacted": True}
        else:
            sanitized[question_id] = _json_value(answer)
    for question_id in sorted(secret_question_ids - sanitized.keys()):
        sanitized[question_id] = {"answersPresent": False, "redacted": True}
    return {"answers": sanitized}


def _question_schema(
    value: Any,
) -> tuple[str | None, dict[str, JsonValue], frozenset[str], frozenset[str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CodexMappingError(
            "invalid_interaction_request",
            "params.questions",
            "Codex requestUserInput questions must be an array",
        )
    properties: dict[str, JsonValue] = {}
    required: list[str] = []
    prompts: list[str] = []
    secret_question_ids: set[str] = set()
    for index, raw_question in enumerate(value):
        question = _mapping(raw_question, f"params.questions[{index}]")
        question_id = _required_string(question.get("id"), f"params.questions[{index}].id")
        if question_id in properties:
            raise CodexMappingError(
                "invalid_interaction_request",
                f"params.questions[{index}].id",
                f"Codex requestUserInput question id {question_id!r} is duplicated",
            )
        is_secret = question.get("isSecret", False)
        if not isinstance(is_secret, bool):
            raise CodexMappingError(
                "invalid_interaction_request",
                f"params.questions[{index}].isSecret",
                "Codex requestUserInput isSecret must be a boolean",
            )
        if is_secret:
            secret_question_ids.add(question_id)
        prompt = _required_text(question.get("question"), f"params.questions[{index}].question")
        header = _required_text(question.get("header"), f"params.questions[{index}].header")
        options = question.get("options")
        labels: list[str] = []
        option_details: list[JsonValue] = []
        if options is not None:
            if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
                raise CodexMappingError(
                    "invalid_interaction_request",
                    f"params.questions[{index}].options",
                    "Codex question options must be an array",
                )
            for option_index, raw_option in enumerate(options):
                option = _mapping(
                    raw_option,
                    f"params.questions[{index}].options[{option_index}]",
                )
                labels.append(
                    _required_text(
                        option.get("label"),
                        f"params.questions[{index}].options[{option_index}].label",
                    )
                )
                option_details.append(_json_value(option))
        property_schema: dict[str, JsonValue] = {
            "type": "string",
            "title": header,
            "description": prompt,
            "x-codex-options": option_details,
            "x-codex-is-secret": is_secret,
            "x-codex-is-other": bool(question.get("isOther", False)),
        }
        if labels:
            property_schema["enum"] = cast(JsonValue, labels)
        properties[question_id] = property_schema
        required.append(question_id)
        prompts.append(prompt)
    return (
        "\n".join(prompts) or None,
        {
            "type": "object",
            "properties": properties,
            "required": cast(JsonValue, required),
        },
        frozenset(properties),
        frozenset(secret_question_ids),
    )


def _approval_decision(value: Any) -> Literal["approved", "rejected", "canceled"]:
    if isinstance(value, str):
        if value in {"accept", "acceptForSession"}:
            return "approved"
        if value == "decline":
            return "rejected"
        if value == "cancel":
            return "canceled"
    elif isinstance(value, Mapping) and len(value) == 1:
        variant = next(iter(value))
        payload = _mapping(value[variant], f"result.decision.{variant}")
        if variant == "acceptWithExecpolicyAmendment":
            amendment = _mapping(
                payload.get("execpolicy_amendment"),
                "result.decision.acceptWithExecpolicyAmendment.execpolicy_amendment",
            )
            command = amendment.get("command")
            if (
                not isinstance(command, Sequence)
                or isinstance(command, (str, bytes))
                or not command
                or any(not isinstance(part, str) or not part for part in command)
            ):
                raise CodexMappingError(
                    "invalid_interaction_response",
                    "result.decision.acceptWithExecpolicyAmendment.execpolicy_amendment.command",
                    "Codex execpolicy amendment command must be a non-empty string array",
                )
            return "approved"
        if variant == "applyNetworkPolicyAmendment":
            amendment = _mapping(
                payload.get("network_policy_amendment"),
                "result.decision.applyNetworkPolicyAmendment.network_policy_amendment",
            )
            _required_string(
                amendment.get("host"),
                "result.decision.applyNetworkPolicyAmendment.network_policy_amendment.host",
            )
            action = _required_string(
                amendment.get("action"),
                "result.decision.applyNetworkPolicyAmendment.network_policy_amendment.action",
            )
            if action not in {"allow", "deny"}:
                raise CodexMappingError(
                    "invalid_interaction_response",
                    "result.decision.applyNetworkPolicyAmendment.network_policy_amendment.action",
                    f"Unsupported network policy amendment action: {action}",
                )
            return "approved"
    raise CodexMappingError(
        "invalid_interaction_response",
        "result.decision",
        f"Unsupported Codex approval decision: {value}",
    )


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise CodexMappingError(
            "invalid_protocol_message",
            field_name,
            f"Codex {field_name} must be text",
        )
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CodexMappingError(
            "invalid_protocol_message",
            field_name,
            f"Codex {field_name} must be a non-negative integer",
        )
    return value


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field_name)


def _string_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CodexMappingError(
            "invalid_item_snapshot",
            field_name,
            f"Codex {field_name} must be an array of text",
        )
    if any(not isinstance(part, str) for part in value):
        raise CodexMappingError(
            "invalid_item_snapshot",
            field_name,
            f"Codex {field_name} must contain only text",
        )
    return tuple(cast(Sequence[str], value))


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return cast(JsonValue, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodexMappingError(
                "non_json_protocol_data",
                "protocol data",
                "Codex protocol data contains a non-finite float",
            )
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise CodexMappingError(
                "non_json_protocol_data",
                "protocol data",
                "Codex protocol object keys must be strings",
            )
        return cast(JsonValue, {key: _json_value(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return cast(JsonValue, [_json_value(item) for item in value])
    raise CodexMappingError(
        "non_json_protocol_data",
        "protocol data",
        f"Codex protocol value is not stably JSON serializable: {type(value).__name__}",
    )


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexMappingError(
            "missing_native_identity",
            field_name,
            f"Codex {field_name} must be a non-empty string",
        )
    return value


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CodexMappingError(
            "invalid_protocol_message",
            field_name,
            f"Codex {field_name} must be an object",
        )
    return value


__all__ = ["CodexAdapterContext", "CodexEventAdapter", "CodexMappingError"]
