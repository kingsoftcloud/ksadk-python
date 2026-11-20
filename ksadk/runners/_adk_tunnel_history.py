"""Keep a failed tunnel turn valid in ADK's own multi-turn model history."""

import logging
from contextlib import aclosing

from ksadk.compat.adk_compat import Event
from ksadk.compat.adk_compat import genai_types as types
from ksadk.tunnel.failure import tunnel_failure
from ksadk.tunnel.protocol import TunnelError

logger = logging.getLogger(__name__)


async def preserve_tunnel_history(events, *, session_service, app_name, user_id, session_id):
    # A resumed invocation may contain older calls. Only close calls emitted by
    # this iterator, in this session, for which ADK did not persist a response.
    call_events = set()
    deferred_error = None
    try:
        async with aclosing(events):
            async for event in events:
                # Workflow-backed ADK emits an error event just before raising
                # the original exception. Give it one iteration to propagate
                # the typed failure; preserve ordinary error-only streams.
                if deferred_error is not None:
                    yield deferred_error
                    deferred_error = None
                if event.error_code in {"TunnelError", "ExceptionGroup"}:
                    deferred_error = event
                    continue
                if not event.partial and event.get_function_calls():
                    call_events.add(event.id)
                yield event
            if deferred_error is not None:
                yield deferred_error
    except Exception as exc:
        failure = tunnel_failure(exc)
        if failure is not None and call_events:
            outcome = failure
            try:
                session = await session_service.get_session(
                    app_name=app_name, user_id=user_id, session_id=session_id
                )
                if session is not None:
                    pending = _pending_calls(session, call_events)
                    outcome = failure.for_unanswered_calls(sum(map(len, pending.values())))
                    await _close_pending_calls(session_service, session, pending, outcome)
            except Exception as storage_error:
                # Persistence failures must not erase the user's outcome-unknown notification.
                logger.warning(
                    "Could not persist ADK tunnel failure (%s)", type(storage_error).__name__
                )
            if outcome != failure:
                raise TunnelError(outcome.result_code, indeterminate=outcome.indeterminate) from exc
        raise


def _pending_calls(session, call_events):
    pending = {}
    for event in session.events:
        if event.partial:
            continue
        scope = (event.invocation_id, event.branch, event.author)
        if event.id in call_events:
            for call in event.get_function_calls():
                if call.id and call.id not in (event.long_running_tool_ids or ()):
                    pending.setdefault(scope, {})[call.id] = call
        if scope in pending:
            for response in event.get_function_responses():
                pending[scope].pop(response.id, None)
    return pending


async def _close_pending_calls(service, session, pending, failure):
    for (invocation_id, branch, author), calls in pending.items():
        if not calls:
            continue
        parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response=failure.tool_result
                )
            )
            for call in calls.values()
        ]
        origin = dict(invocation_id=invocation_id, branch=branch, author=author)
        await service.append_event(
            session, Event(**origin, content=types.Content(role="user", parts=parts))
        )
        await service.append_event(
            session,
            Event(
                **origin,
                content=types.Content(role="model", parts=[types.Part(text=failure.message)]),
            ),
        )
