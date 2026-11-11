"""SDK UI proxy; the credential endpoint is never reachable as a browser Action."""

from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.invocation_identity import resolve_trusted_invocation_identity

from .client import BrokerError
from .security import digest, identity


class AuthorizationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    WaitId: str = Field(min_length=1, max_length=64)
    ExpectedRevision: int | None = Field(default=None, ge=1)
    AgentId: str | None = None
    SessionId: str | None = None
    IdempotencyKey: str | None = None


def install_public_routes(app, extension):
    def register(name):
        async def action(
            body: AuthorizationAction,
            context: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
        ):
            if not context.is_complete:
                return JSONResponse(
                    {"Code": 403, "ReasonCode": "IDENTITY_REQUIRED"}, status_code=403
                )
            try:
                data = await extension.client.call(
                    "BrowserMcpAuthorization",
                    public_action=name,
                    wait_id=body.WaitId,
                    expected_revision=body.ExpectedRevision,
                    agent_id=body.AgentId,
                    session_id=body.SessionId,
                    subject_digest=digest(identity(context.to_payload())),
                )
                return JSONResponse(
                    {"Code": 0, "Message": "Success", "Data": data},
                    headers={"Cache-Control": "no-store"},
                )
            except BrokerError as exc:
                return JSONResponse(
                    {"Code": 409, "ReasonCode": exc.code, "Message": exc.code},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )

        app.add_api_route("/agentengine/api/v1/" + name, action, methods=["POST"], name=name)

    for name in [
        "GetMcpAuthorizationWait",
        "OpenMcpAuthorization",
        "RetryMcpAuthorization",
        "RetryMcpConnection",
        "CancelMcpAuthorizationWait",
    ]:
        register(name)
