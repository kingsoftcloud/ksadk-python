"""Local Interaction/v1 control admission, sharing the existing Kernel Inbox."""

from typing import Any, Literal

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from ksadk.interaction.schema import require_json
from ksadk.kernel import ingress
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.factory import get_runtime_execution, get_state
from ksadk.server.invocation_identity import resolve_trusted_invocation_identity

from . import dependencies as deps
from .common import _action_response
from .control import _require_control_session
from .routers import control_router


class SubmitInteractionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    AgentId: str
    UserId: str | None = None
    SessionId: str
    RunId: str
    InteractionId: str
    ExpectedRevision: int = Field(ge=1, strict=True)
    InteractionAction: Literal["approve", "reject", "submit", "cancel"]
    Response: JsonValue = None
    IdempotencyKey: str = Field(min_length=1, max_length=256)

    @model_validator(mode="before")
    @classmethod
    def action_and_presence(cls, value: Any):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        # Legacy local Web sent Action in the body. The KOP operation name
        # remains separate; never interpret SubmitInteraction as a decision.
        outer = value.pop("Action", None)
        if "InteractionAction" not in value and outer in {"approve", "reject", "submit", "cancel"}:
            value["InteractionAction"] = outer
        if value.get("InteractionAction") == "submit" and "Response" not in value:
            raise ValueError("Response must be present for structured submissions")
        return value

    @field_validator("Response", mode="before")
    @classmethod
    def json_response(cls, value):
        return require_json(value)


@control_router.post("/agentengine/api/v1/SubmitInteraction")
async def submit_interaction_action(
    request: SubmitInteractionActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    if ingress.authority_mode() != "local":
        # Hosted commands require Server-issued permits on the Kernel ingress.
        # A local compatibility endpoint must never self-sign hosted admission.
        raise HTTPException(409, detail="Interaction/v1 requires an active local Kernel")
    await _require_control_session(
        deps.resolve_session_service(),
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
        invocation_identity=invocation_identity,
    )
    _, launch = get_runtime_execution()
    if not ingress.interaction_route_active(launch_context=launch):
        local = get_state().local_interactions
        if local is not None:
            await local.ensure()
    if not ingress.interaction_route_active(launch_context=launch):
        raise HTTPException(409, detail="Interaction/v1 requires an active local Kernel")
    trusted = ingress.trusted_context(
        source_kind="agui",
        source_ref=request.IdempotencyKey,
        session_id=request.SessionId,
        operations=("submit_interaction",),
        launch_context=launch,
    )
    payload = {
        "run_id": request.RunId,
        "interaction_id": request.InteractionId,
        "expected_revision": request.ExpectedRevision,
        "action": request.InteractionAction,
        "response": request.Response,
        "token_ref": trusted.permit.permit_id,
    }
    command = ingress.map_control_request(
        command_type="submit_interaction",
        session_id=request.SessionId,
        run_id=request.RunId,
        idempotency_key=request.IdempotencyKey,
        payload=payload,
        trusted=trusted,
    )
    receipt = await ingress.submit_command(command, permit=trusted.permit)
    return JSONResponse(
        status_code=ingress.receipt_http_status(receipt),
        headers=ingress.receipt_response_headers(receipt),
        content=_action_response("SubmitInteraction", receipt.model_dump(mode="json")),
    )
