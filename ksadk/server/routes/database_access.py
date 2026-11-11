"""Read-only database access diagnostics for the protected management route."""

from fastapi import Request

from ..database_access import check_database_access
from ..database_access_contract import ACTION, PATH, DatabaseAccessRequest, DatabaseAccessResponse
from .common import _action_response
from .routers import database_access_router


@database_access_router.post(
    PATH, response_model=DatabaseAccessResponse, response_model_exclude_none=False
)
async def check_agent_database_access(request: Request, body: DatabaseAccessRequest):
    request_id = request.state.database_access_request_id
    data = await check_database_access(body, request_id=request_id)
    return _action_response(ACTION, data, request_id=request_id)
