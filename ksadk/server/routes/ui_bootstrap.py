"""UI capability/read model for the Runtime's existing bootstrap endpoint."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ksadk.kernel import ingress as _kernel_ingress
from ksadk.runtime.executor import project_legacy_runtime_capabilities
from ksadk.sessions.persistence import persistence_probe_timeout
from ksadk.tools.gateway import tool_approval_capability
from ksadk.toolsets import describe_agentengine_tools
from ksadk_runtime_common.workspace_files import (
    build_workspace_files_bootstrap,
    workspace_files_enabled,
)

from . import dependencies as deps
from .common import (
    _action_response,
    _build_bootstrap_model_payload,
    _build_native_terminal_capability,
    _resolve_agent_ui_spec,
)
from .models import _runtime_agent_id


def _submit_interaction_route_available(app):
    from starlette.routing import NoMatchFound

    try:
        return str(app.url_path_for("submit_interaction_action")) == "/agentengine/api/v1/SubmitInteraction"
    except NoMatchFound:
        return False


async def build_ui_bootstrap(request, invocation_identity, *, state, executor, launch_context):
    detection = launch_context.detection
    agent_id = request.AgentId or _runtime_agent_id(launch_context)
    description = str(getattr(detection, "description", "") or "")
    framework = launch_context.runtime_type.strip().lower()
    workspace_enabled = workspace_files_enabled(default=True)
    ui_spec = _resolve_agent_ui_spec()
    if state.local_interactions is not None:
        try:
            async with asyncio.timeout(persistence_probe_timeout()):
                await state.local_interactions.prepare()
                if state.local_interactions.oauth is not None:
                    await state.local_interactions.ensure()
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Local input capability preparation failed (%s)", type(exc).__name__,
            )
    capability_subject = executor.capability_subject(launch_context)
    if capability_subject is not None:
        wait_timeout = persistence_probe_timeout()
        capability_snapshot = await state.persistence_capability.get_snapshot(
            runner=capability_subject,
            framework=framework,
            status_provider=deps.get_persistence_status,
            session_service_provider=deps.resolve_session_service,
            wait_timeout=wait_timeout,
        )
        persistence = dict(capability_snapshot.session_persistence)
        checkpoint_persistence = dict(capability_snapshot.checkpoint_persistence)
        runtime_capabilities = dict(capability_snapshot.runtime_capabilities)
        runtime_capability_matrix = executor.capability_matrix(launch_context)
    else:
        runtime_capability_matrix = executor.capability_matrix(launch_context)
        persistence_status = dict(await deps.get_persistence_status(framework=framework))
        if isinstance(persistence_status.get("Session"), Mapping):
            persistence = dict(persistence_status["Session"])
            checkpoint_persistence = dict(persistence_status.get("Checkpoint") or {})
        else:
            persistence = persistence_status
            checkpoint_persistence = dict(persistence_status)
        runtime_capabilities = project_legacy_runtime_capabilities(
            executor.native_capabilities(launch_context), runtime_capability_matrix
        )
    # This is the configured shared-store probe, not the effective native saver.
    # In local debugging it can be unconfigured while RuntimeCapabilities below
    # correctly reports a durable, pod-local SQLite checkpoint.
    for shared_status in (persistence, checkpoint_persistence):
        shared_status.setdefault("ReadinessScope", "shared_postgres")
    resume_capability = (
        runtime_capabilities.get("ResumeRun") if isinstance(runtime_capabilities, Mapping) else None
    )
    resume_capability = resume_capability if isinstance(resume_capability, Mapping) else {}
    checkpoint_capability = (
        runtime_capabilities.get("Checkpoint")
        if isinstance(runtime_capabilities, Mapping)
        else None
    )
    checkpoint_capability = (
        checkpoint_capability if isinstance(checkpoint_capability, Mapping) else {}
    )
    cancel_capability = (
        runtime_capabilities.get("CancelRun") if isinstance(runtime_capabilities, Mapping) else None
    )
    cancel_capability = cancel_capability if isinstance(cancel_capability, Mapping) else {}
    checkpoint_resume_capability = {
        "Supported": bool(resume_capability.get("Supported")),
        "Checkpoint": checkpoint_capability,
        "ResumeRun": resume_capability,
    }
    checkpoint_resume_supported = bool(checkpoint_resume_capability["Supported"])
    cancel_run_supported = bool(cancel_capability.get("Supported"))
    responses_transport = {
        "Protocol": "responses",
        "Runtime": "ksadk",
        "Endpoint": "/v1/responses",
        "Version": "v1",
        "Capabilities": {
            "A2UI": False,
            "Interrupt": checkpoint_resume_supported,
            "Cancel": cancel_run_supported,
        },
    }
    hosted_chat_transports = []
    if state.agui_agent is not None and state.agui_config is not None:
        from ksadk.agui.config import AGUI_PROTOCOL_VERSION

        hosted_chat_transports.append(
            {
                "Protocol": "ag-ui",
                "Runtime": "copilotkit",
                "Endpoint": state.agui_config.path,
                "Version": AGUI_PROTOCOL_VERSION,
                "Capabilities": {
                    "A2UI": True,
                    "Interrupt": checkpoint_resume_supported,
                    "Cancel": True,
                    **({"SessionEventReplay": True} if (
                        _kernel_ingress.kernel_route_active()
                        or (state.local_interactions is not None
                            and state.local_interactions.prepared)
                    ) else {}),
                },
            }
        )
    hosted_chat_transports.append(responses_transport)
    preferred_transport = (
        "ag-ui"
        if hosted_chat_transports[0]["Protocol"] == "ag-ui"
        and bool(getattr(state.agui_config, "preferred", False))
        else "responses"
    )
    # Producer declaration is separate from transport availability. Consumers
    # intersect this surface with their renderer catalogue before selecting a
    # presentation profile; an older runtime has no such declaration.
    presentation_capabilities: dict[str, Any] = {}
    managed_remote_surface = framework in {"codex", "langgraph"} and bool(
        getattr(state.a2a_bootstrap, "outbound_enabled", False)
    )
    if framework == "adk" or managed_remote_surface:
        from ksadk.conversations.contracts import ConversationCapability, ConversationSurface

        presentation_capabilities["ConversationSurface"] = ConversationSurface(
            surface_id=f"runtime:{agent_id}",
            session_id=request.SessionId or "new-session",
            provider_ref=framework,
            inputs=(ConversationCapability(name="ksadk.presentation", mode="native"),),
            outputs=(
                ConversationCapability(
                    name="agent.block",
                    mode="native" if framework == "adk" else "translated",
                ),
            ),
        ).model_dump(mode="json", by_alias=True)
    from ksadk.kernel.bootstrap import get_agent_kernel_runtime
    from ksadk.mcp_runtime.oauth.contract import CAPABILITY as MCP_AUTH_CAPABILITY
    kernel_runtime = get_agent_kernel_runtime()
    oauth_ready = bool(kernel_runtime and kernel_runtime.worker._mcp_authorization)
    return _action_response(
        "GetAgentUiBootstrap",
        {
            "Agent": {
                "AgentId": agent_id,
                "Name": str(getattr(detection, "name", "") or agent_id),
                "Description": description or "",
                "Framework": framework,
            },
            "Modules": ["Chat", "Build", "Deploy"],
            "Capabilities": {
                # Lightweight protocol negotiation; it does not hydrate a
                # session or require historical Agent packages to be patched.
                "HistoryRead": {
                    "Protocol": "ksadk.history/v2",
                    "SessionContractVersion": 2,
                    "SupportsEpoch": True,
                    "SupportsSnapshot": True,
                },
                **presentation_capabilities,
                "Attachments": True,
                "WorkspaceFiles": workspace_enabled,
                "Approval": True,
                "InteractionV1": _kernel_ingress.interaction_route_active(
                    launch_context=launch_context,
                )
                and _submit_interaction_route_available(state.app),
                "ApprovalPolicy": tool_approval_capability(),
                "Thinking": True,
                "StopRun": cancel_run_supported,
                "ResumeRun": checkpoint_resume_supported,
                "Persistence": persistence,
                "CheckpointPersistence": checkpoint_persistence,
                "RuntimeCapabilities": runtime_capabilities,
                "RuntimeCapabilityMatrix": runtime_capability_matrix,
                "CheckpointResumeCapability": checkpoint_resume_capability,
                "RunLifecycle": {
                    "Enabled": True,
                    "Resume": True,
                    "Abort": True,
                    "Checkpoints": checkpoint_resume_supported,
                    "CheckpointResume": checkpoint_resume_supported,
                    "CheckpointResumePreview": checkpoint_resume_supported,
                },
                "MCP": False,
                **({"McpAuthorization": MCP_AUTH_CAPABILITY} if oauth_ready else {}),
                "HostedRuntime": False,
                "NativeTerminal": _build_native_terminal_capability(framework, invocation_identity),
                "BuiltinTools": describe_agentengine_tools(),
            },
            "WorkspaceFiles": build_workspace_files_bootstrap(enabled=workspace_enabled),
            "AccessMode": "Owner",
            "SharePermissions": {
                "Interactive": True,
                "DefaultPath": ui_spec.get("ui_path") or ui_spec.get("path") or "/chat",
                "SharePath": ui_spec.get("ui_path") or ui_spec.get("path") or "/chat",
            },
            "CustomUI": {
                "Enabled": bool(ui_spec.get("enabled")),
                "Profile": ui_spec.get("ui_profile") or ui_spec.get("profile"),
                "Path": ui_spec.get("ui_path") or ui_spec.get("path"),
                "Url": ui_spec.get("ui_url") or ui_spec.get("url"),
                "BundlePath": ui_spec.get("ui_bundle_path") or ui_spec.get("bundle_path"),
            },
            "ApiFormats": ["responses", "chat_completions"],
            "HostedChat": {
                "PreferredTransport": preferred_transport,
                "Transports": hosted_chat_transports,
            },
            "Stream": True,
            "SessionId": request.SessionId,
            "SessionBackend": deps.describe_session_backend(),
            "HostedRuntime": None,
            "Model": _build_bootstrap_model_payload(),
        },
    )
