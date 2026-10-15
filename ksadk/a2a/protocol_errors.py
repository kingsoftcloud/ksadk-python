"""A2A 1.0 error binding corrections for the pinned a2a-sdk 1.1 handler.

Keep the upstream transport/authentication path, correct terminal-task errors,
and persist confirmed Kernel cancellation after an upstream queue-close race.
No global SDK patching.
"""

import json
from functools import wraps

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.request_handler import validate_request_params
from a2a.types import TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, UnsupportedOperationError
from starlette.responses import JSONResponse
from starlette.routing import Route

_CONFIRMED_KERNEL_CANCEL = "ksadk.confirmed_kernel_cancel"

_TERMINAL = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}


class ProtocolRequestHandler(DefaultRequestHandler):
    async def _reconcile_owned_task(self, task_id, context):
        task = await self.task_store.get(task_id, context)
        if task is None:
            raise TaskNotFoundError()
        reconcile = getattr(self, "terminal_task_reconciler", None)
        if task.status.state not in _TERMINAL and reconcile is not None:
            await reconcile(task, context, self.task_store)
            task = await self.task_store.get(task_id, context)
            if task is None:
                raise TaskNotFoundError()
        return task

    @validate_request_params
    async def on_get_task(self, params, context):
        await self._reconcile_owned_task(params.id, context)
        return await super().on_get_task(params, context)

    @validate_request_params
    async def on_cancel_task(self, params, context):
        # ActiveTask may cache a Task; authorize this request against durable ownership.
        if await self.task_store.get(params.id, context) is None:
            raise TaskNotFoundError()
        context.state.pop(_CONFIRMED_KERNEL_CANCEL, None)
        try:
            result = await super().on_cancel_task(params, context)
            confirmed = context.state.get(_CONFIRMED_KERNEL_CANCEL)
            if confirmed is not None and confirmed[0] == params.id:
                # Upstream V2 cancels its producer before invoking executor.cancel.
                # That can close the event queue before the Kernel acknowledgement.
                # Wait for upstream cleanup, then persist only this confirmed fact.
                current = await self.task_store.get(params.id, context)
                if current is None:
                    raise TaskNotFoundError()
                if current.status.state not in _TERMINAL:
                    repair = getattr(self.task_store, "save_confirmed_cancellation", None)
                    if repair is not None:
                        current = await repair(current, confirmed[1], context)
                        if current is None:
                            raise TaskNotFoundError()
                    else:
                        current.status.CopyFrom(confirmed[1])
                        await self.task_store.save(current, context)
                return current
            return result
        finally:
            context.state.pop(_CONFIRMED_KERNEL_CANCEL, None)

    async def on_subscribe_to_task(self, params, context):
        task = await self._reconcile_owned_task(params.id, context)
        if task.status.state in _TERMINAL:
            raise UnsupportedOperationError("Task is already terminal")
        try:
            async for event in super().on_subscribe_to_task(params, context):
                yield event
        except InvalidParamsError:
            # V2 ActiveTask reports a terminal subscription as invalid params.
            # Confirm the same owned task before translating the error.
            task = await self.task_store.get(params.id, context)
            if task is not None and task.status.state in _TERMINAL:
                raise UnsupportedOperationError("Task is already terminal") from None
            raise


def correct_cancel_rest_route(route):
    if not isinstance(route, Route) or not route.path.endswith("/tasks/{id}:cancel"):
        return route
    original = route.endpoint

    @wraps(original)
    async def endpoint(request):
        response = await original(request)
        if response.status_code == 400 and isinstance(response, JSONResponse):
            body = json.loads(response.body)
            error = body.get("error", {})
            if any(
                detail.get("reason") == "TASK_NOT_CANCELABLE" for detail in error.get("details", [])
            ):
                error["code"] = 409
                return JSONResponse(body, status_code=409)
        return response

    return Route(
        route.path,
        endpoint,
        methods=route.methods,
        name=route.name,
        include_in_schema=route.include_in_schema,
    )
