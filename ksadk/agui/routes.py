"""Factory wiring for the official AG-UI FastAPI endpoint helper."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.agui.config import AGUIConfig, require_agui_dependencies
from ksadk.runtime.adapter import RuntimeLaunchContext
from ksadk.runtime.executor import RuntimeExecutor


def _fastapi_endpoint_helper() -> Callable[..., Any]:
    """Resolve the optional untyped helper only after the dependency gate."""
    module = import_module("ag_ui_langgraph")
    helper = getattr(module, "add_langgraph_fastapi_endpoint", None)
    if not callable(helper):
        raise RuntimeError("ag-ui-langgraph does not expose add_langgraph_fastapi_endpoint")
    return cast(Callable[..., Any], helper)


def add_ksadk_agui_endpoint(
    app: FastAPI,
    executor: RuntimeExecutor,
    launch_context: RuntimeLaunchContext,
    config: AGUIConfig,
    *,
    event_store_factory=None,
    session_service_factory=None,
    local_kernel_provider=None,
) -> KsadkAGUIAgent:
    require_agui_dependencies()

    agent = KsadkAGUIAgent(
        name=config.agent_name,
        executor=executor,
        launch_context=launch_context,
        event_store_factory=event_store_factory,
        session_service_factory=session_service_factory,
        local_kernel_provider=local_kernel_provider,
    )
    from ksadk.agui.hosted import ADMISSION_HEADER, PREPARE_PATH, VERSION_HEADER
    from ksadk.agui.request_identity import agui_admission, agui_request_identity
    from ksadk.server.invocation_identity import resolve_trusted_invocation_identity

    async def identity_scope(
        request: Request, identity=Depends(resolve_trusted_invocation_identity)
    ):
        import json

        raw = request.headers.get(ADMISSION_HEADER)
        admission = None
        if raw is not None:
            try:
                if len(raw) > 65536:
                    raise ValueError("oversized admission")
                admission = json.loads(raw)
                if not isinstance(admission, dict):
                    raise ValueError("invalid admission")
            except ValueError:
                raise HTTPException(403, "Invalid AG-UI admission") from None
        token = agui_request_identity.set(identity)
        receipt_token = agui_admission.set(admission)
        try:
            yield
        finally:
            agui_admission.reset(receipt_token)
            agui_request_identity.reset(token)

    router = APIRouter(dependencies=[Depends(identity_scope)])
    _fastapi_endpoint_helper()(router, cast(Any, agent), path=config.path)
    app.include_router(router)

    @app.post(PREPARE_PATH, include_in_schema=False)
    async def prepare_hosted_agui(
        request: Request, identity=Depends(resolve_trusted_invocation_identity)
    ):
        from ag_ui.core import RunAgentInput
        from pydantic import ValidationError

        from ksadk.agui._agent_resume import AguiRootNullUnsupported
        from ksadk.agui.hosted import prepare
        from ksadk.interaction.schema import InputValidationError
        from ksadk.kernel.bootstrap import get_agent_kernel_runtime
        from ksadk.kernel.ingress import authority_mode

        # This read-only preparation never submits a command. Non-LangGraph
        # runtimes keep their existing native AG-UI route.
        if (
            config.runtime_type != "langgraph"
            or get_agent_kernel_runtime() is None
            or authority_mode() != "hosted"
        ):
            return JSONResponse({"detail": "Unsupported AG-UI runtime"}, status_code=404)
        headers = {VERSION_HEADER: "1"}
        try:
            tenant_id = request.headers.get("x-auth-account-id", "").strip()
            if identity.is_empty or not tenant_id:
                raise HTTPException(401, "Verified platform account and identity are required")
            input = RunAgentInput.model_validate(await request.json())
            bound = agent.clone()
            bound._identity = identity
            await bound._authorize_session(input)
            return JSONResponse(
                await prepare(bound, input, tenant_id=tenant_id),
                headers=headers,
            )
        except HTTPException as exc:
            return JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=headers
            )
        except (ValidationError, ValueError, InputValidationError, AguiRootNullUnsupported):
            return JSONResponse(
                {"detail": "Invalid AG-UI request or answer"}, status_code=422, headers=headers
            )

    return agent


__all__ = ["add_ksadk_agui_endpoint"]
