"""CodexEventAdapter 的交互(interaction/serverRequest)映射方法（纯移动自 codex，行为不变）。

以 mixin 形式被 :class:`CodexEventAdapter` 继承。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable, Literal

from pydantic import JsonValue

from ksadk.events.adapters._codex_items import (
    _APPROVAL_KINDS,
    _CONTROL_REQUEST_BUILDERS,
    CodexAdapterContext,
    _approval_response,
    _elicitation_request,
    _envelope,
    _InteractionState,
    _protocol_source,
    _structured_response_data,
    _thread_continuation_identity,
)
from ksadk.events.adapters._codex_validators import (
    _fail,
    _json_value,
    _mapping,
    _question_schema,
    _request_id,
    _required_string,
    _required_text,
)
from ksadk.events.canonical import (
    ApprovalRequest,
    ApprovalResponse,
    InteractionRequest,
    InteractionRequested,
    InteractionResolved,
    InteractionResponse,
    RunInterrupted,
    RunProgress,
    RuntimeEvent,
    SourceRef,
    StructuredInputRequest,
    StructuredInputResponse,
)
from ksadk.events.identity import stable_item_id, stable_scope_id


class _CodexInteractionMixin:
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
            _fail(
                "interaction_already_pending",
                "id",
                f"Codex JSON-RPC request {request_id!r} is already pending",
            )
        thread_id = f"runtime:{context.run_id}"
        turn_id = "control"
        scope_id = stable_scope_id("codex", thread_id, turn_id)
        interaction_id = stable_item_id("codex", scope_id, "interaction", method, request_id)
        request = _CONTROL_REQUEST_BUILDERS[method](params)
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
                **_envelope(context, cursor, timestamp)(
                    scope_id, interaction_id, "interaction.requested", "structured_input", source
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
            _fail(
                "interaction_scope_mismatch",
                "params.threadId",
                "Codex serverRequest/resolved threadId does not match the pending request",
            )
        resolved = self._resolve_interaction(
            state,
            cursor=cursor,
            timestamp=timestamp,
            context=context,
            source=_protocol_source(
                method="serverRequest/resolved",
                cursor=cursor,
                thread_id=state.thread_id,
                turn_id=state.turn_id,
                native_item_id=state.native_item_id,
                native_event_id=request_id,
            ),
            response=(
                ApprovalResponse(
                    decision="canceled",
                    data={"source": "serverRequest/resolved", "requestId": request_id},
                )
                if state.interaction_kind == "approval"
                else StructuredInputResponse(
                    data={"source": "serverRequest/resolved", "requestId": request_id}
                )
            ),
        )
        return (resolved,)

    def _map_interaction_request(
        self,
        *,
        message: Mapping[str, Any],
        method: str,
        params: Mapping[str, Any],
        env: Callable[..., dict[str, Any]],
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
            _fail(
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
            request = _elicitation_request(params)
        else:
            kind = "approval"
            request = ApprovalRequest(
                call_id=native_item_id,
                kind=_APPROVAL_KINDS[method],
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
            **env(scope_id, interaction_id, "interaction.requested", kind, source),
            interaction_id=interaction_id,
            interaction_kind=kind,
            request=request,
        )
        if not interrupts_run:
            return (requested,)
        return (
            requested,
            RunInterrupted(
                **env(scope_id, turn_id, "run.interrupted", interaction_id, source),
                status="interrupted",
                reason="Codex requires user interaction",
                interaction_id=interaction_id,
                continuation_id=self._thread_continuations.setdefault(
                    thread_id, _thread_continuation_identity(thread_id)[1]
                ),
            ),
        )

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
                _fail(
                    "thread_resume_failed",
                    "error",
                    "Codex thread/resume failed with a JSON-RPC error",
                )
            _mapping(message.get("result"), "result")
            return ()
        state = self._interactions.get(request_id)
        if state is None:
            _fail(
                "unknown_jsonrpc_response",
                "id",
                f"Codex response has no pending request: {request_id}",
            )
        is_error_response = "error" in message
        result: Mapping[str, Any] = {}
        response: InteractionResponse
        if is_error_response:
            error = _mapping(message.get("error"), "error")
            code = error.get("code")
            if isinstance(code, bool) or not isinstance(code, int):
                _fail(
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
                    decision="canceled", data={"jsonrpcError": sanitized_error}
                )
            else:
                response = StructuredInputResponse(data={"jsonrpcError": sanitized_error})
        else:
            result = _mapping(message.get("result"), "result")
            if state.interaction_kind == "approval":
                response = _approval_response(state.method, result)
            else:
                response = StructuredInputResponse(data=_structured_response_data(state, result))
        source = _protocol_source(
            method="jsonrpc/response",
            cursor=cursor,
            thread_id=state.thread_id,
            turn_id=state.turn_id,
            native_item_id=state.native_item_id,
            native_event_id=request_id,
        )
        resolved = self._resolve_interaction(
            state,
            cursor=cursor,
            timestamp=timestamp,
            context=context,
            source=source,
            response=response,
        )
        if is_error_response:
            return (resolved,)
        resumes = (
            isinstance(response, ApprovalResponse) and response.decision in {"approved", "rejected"}
        ) or (
            isinstance(response, StructuredInputResponse)
            and (
                state.method == "item/tool/requestUserInput"
                or result.get("action") in {"accept", "decline"}
            )
        )
        if not resumes or not state.interrupts_run:
            return (resolved,)
        return (
            resolved,
            RunProgress(
                **_envelope(context, cursor, timestamp)(
                    state.scope_id, state.turn_id, "run.progress", state.interaction_id, source
                ),
                status="running",
                message="Codex user interaction resolved; turn resumed",
            ),
        )

    def _resolve_interaction(
        self,
        state: _InteractionState,
        *,
        cursor: str,
        timestamp: float,
        context: CodexAdapterContext,
        source: SourceRef,
        response: InteractionResponse,
    ) -> InteractionResolved:
        resolved = InteractionResolved(
            **_envelope(context, cursor, timestamp)(
                state.scope_id,
                state.interaction_id,
                "interaction.resolved",
                state.interaction_kind,
                source,
            ),
            interaction_id=state.interaction_id,
            interaction_kind=state.interaction_kind,
            response=response,
        )
        del self._interactions[state.request_id]
        return resolved
