"""Local native Run ownership checks for established HTTP compatibility routes."""

import hashlib

from fastapi import HTTPException

from ksadk.kernel import ingress
from ksadk.kernel.bootstrap import get_agent_kernel_runtime
from ksadk.kernel.contracts import AgentStatusQuery

from .common import _action_response


def _context(launch, session_id, ref, operations):
    return ingress.trusted_context(
        source_kind="agui",
        source_ref=ref,
        session_id=session_id,
        operations=operations,
        launch_context=launch,
    )


def _query(trusted, session_id):
    return AgentStatusQuery(
        tenant_id=trusted.tenant_id,
        agent_instance_id=trusted.agent_instance_id,
        session_id=session_id,
        authorization_ref=trusted.permit.permit_id,
    )


async def require_idle_local_session(launch, session_id):
    runtime = get_agent_kernel_runtime()
    if not (
        runtime is not None
        and getattr(runtime.config, "local_interactions", False)
        and runtime.config.launch_context is launch
    ):
        return
    trusted = _context(launch, session_id, "compatibility-start", ("get_status",))
    status = await runtime.kernel.status(_query(trusted, session_id), permit=trusted.permit)
    if status.active_run_id is not None:
        raise HTTPException(
            409, detail="Submit or cancel the pending interaction before a new turn"
        )


async def cancel_local_kernel_run(launch, session_id, public_ref):
    if not ingress.interaction_route_active(launch_context=launch):
        return None
    trusted = _context(launch, session_id, public_ref, ("get_status", "interrupt"))
    kernel = ingress.get_agent_kernel()
    run_id = await kernel.public_run_id(
        _query(trusted, session_id),
        public_ref,
        permit=trusted.permit,
    )
    if run_id is None:
        return None
    command = ingress.map_control_request(
        command_type="interrupt",
        session_id=session_id,
        idempotency_key="cancel:" + hashlib.sha256(public_ref.encode()).hexdigest(),
        trusted=trusted,
        run_id=run_id,
        payload={"run_id": run_id},
    )
    receipt = await ingress.submit_command(command, permit=trusted.permit)
    return _action_response(
        "CancelRun",
        {
            "Cancelled": receipt.status in {"accepted", "duplicate"},
            "Found": True,
            "Status": "cancelling"
            if receipt.status in {"accepted", "duplicate"}
            else receipt.status,
            "RuntimeCancelStatus": receipt.status,
        },
    )
