"""A2A 协议数据面 (goal-05 清洁重写,wire 1.0 / a2a-sdk 1.1.0)。

旧 0.3 demo(``A2AStarletteApplication`` + ``InMemoryTaskStore`` + 顶层 ``url``)
已整体推倒,不保留兼容。本包:

- :mod:`ksadk.a2a.card` — wire 1.0 AgentCard(``supportedInterfaces``)。
- :mod:`ksadk.a2a.task_store` — durable ``DatabaseTaskStore``(表 ``ksadk_a2a_tasks``)。
- :mod:`ksadk.a2a.executor` — runner → A2A 请求生命周期桥接。
- :mod:`ksadk.a2a.task_adapter` — A2A Task ↔ Runtime 映射(§7.2),cancel 走 RuntimeAdapter。
- :mod:`ksadk.a2a.event_adapter` — A2A Message/Task/Artifact ↔ RuntimeEvent。
- :mod:`ksadk.a2a.server` — ``A2AProtocolServer``(route factories + handler + store)。
- :mod:`ksadk.a2a.routes` — ``add_a2a_protocol_routes`` 装配进 create_runtime_app。

client 侧(``A2ASpaceClient`` 动态发现)见 goal-06。

Public exports load their implementation only when requested. Importing a pure
protocol submodule must not initialize optional framework or PostgreSQL adapters.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "product_key_http_client": ("ksadk.a2a.product_key", "product_key_http_client"),
    "A2AConfig": ("ksadk.a2a.routes", "A2AConfig"),
    "A2AApiKeyPrincipal": ("ksadk.a2a.identity", "A2AApiKeyPrincipal"),
    "A2AAuthnMode": ("ksadk.a2a.identity", "A2AAuthnMode"),
    "A2A_AUTHN_MODES": ("ksadk.a2a.identity", "A2A_AUTHN_MODES"),
    "A2ACheckpointStore": ("ksadk.a2a.bootstrap", "A2ACheckpointStore"),
    "A2AContextIdentity": ("ksadk.a2a.context_store", "A2AContextIdentity"),
    "A2AContextStore": ("ksadk.a2a.context_store", "A2AContextStore"),
    "A2AControlPlane": ("ksadk.a2a.control_plane", "A2AControlPlane"),
    "A2AControlPlaneError": ("ksadk.a2a.control_plane", "A2AControlPlaneError"),
    "A2ACancelResult": ("ksadk.a2a.invocation_types", "A2ACancelResult"),
    "A2AContentPolicy": ("ksadk.a2a.invocation_types", "A2AContentPolicy"),
    "A2AContinuationRequest": ("ksadk.a2a.invocation_types", "A2AContinuationRequest"),
    "A2AInternalAction": ("ksadk.a2a.control_plane", "A2AInternalAction"),
    "A2AInvocationContext": ("ksadk.a2a.invocation_types", "A2AInvocationContext"),
    "A2AInvocationError": ("ksadk.a2a.invocation_types", "A2AInvocationError"),
    "A2AInvocationHandle": ("ksadk.a2a.invocation_types", "A2AInvocationHandle"),
    "A2AInvocationMessage": ("ksadk.a2a.invocation_types", "A2AInvocationMessage"),
    "A2AInvocationOutputRef": ("ksadk.a2a.invocation_types", "A2AInvocationOutputRef"),
    "A2AInvocationRequest": ("ksadk.a2a.invocation_types", "A2AInvocationRequest"),
    "A2AInvocationSnapshot": ("ksadk.a2a.invocation_types", "A2AInvocationSnapshot"),
    "A2A_INTERNAL_ACTIONS": ("ksadk.a2a.control_plane", "A2A_INTERNAL_ACTIONS"),
    "A2A_INTERNAL_PATH_PREFIX": ("ksadk.a2a.control_plane", "A2A_INTERNAL_PATH_PREFIX"),
    "A2AOperation": ("ksadk.a2a.control_plane", "A2AOperation"),
    "A2AEventAdapter": ("ksadk.a2a.event_adapter", "A2AEventAdapter"),
    "A2AExternalTransport": ("ksadk.a2a.external_transport", "A2AExternalTransport"),
    "A2AGatewayIdentityMiddleware": ("ksadk.a2a.identity", "A2AGatewayIdentityMiddleware"),
    "A2AIngressIdentity": ("ksadk.a2a.identity", "A2AIngressIdentity"),
    "A2AIngressTargetBinding": ("ksadk.a2a.identity", "A2AIngressTargetBinding"),
    "A2APlatformTask": ("ksadk.a2a.space_client", "A2APlatformTask"),
    "A2AProtocolServer": ("ksadk.a2a.server", "A2AProtocolServer"),
    "A2ARuntimeExecutor": ("ksadk.a2a.executor", "A2ARuntimeExecutor"),
    "A2ARuntimeTaskAdapter": ("ksadk.a2a.task_adapter", "A2ARuntimeTaskAdapter"),
    "A2AResumeState": ("ksadk.a2a.resume_store", "A2AResumeState"),
    "A2AResumePayloadKind": ("ksadk.a2a.resume_store", "A2AResumePayloadKind"),
    "A2AResumeStateStore": ("ksadk.a2a.resume_store", "A2AResumeStateStore"),
    "A2ASpaceClient": ("ksadk.a2a.space_client", "A2ASpaceClient"),
    "A2AStreamEvent": ("ksadk.a2a.langgraph", "A2AStreamEvent"),
    "A2A_PROTOCOL_VERSION": ("ksadk.a2a.card", "A2A_PROTOCOL_VERSION"),
    "A2ARoute": ("ksadk.a2a.control_plane", "A2ARoute"),
    "A2ARouteOpener": ("ksadk.a2a.external_transport", "A2ARouteOpener"),
    "A2ARouteInterface": ("ksadk.a2a.control_plane", "A2ARouteInterface"),
    "A2A_TASK_TABLE": ("ksadk.a2a.task_store", "A2A_TASK_TABLE"),
    "A2ATarget": ("ksadk.a2a.control_plane", "A2ATarget"),
    "A2ATaskEventBatch": ("ksadk.a2a.task_event_outbox", "A2ATaskEventBatch"),
    "A2ATaskEventDispatcher": ("ksadk.a2a.task_event_dispatcher", "A2ATaskEventDispatcher"),
    "A2ATaskEventSink": ("ksadk.a2a.task_event_dispatcher", "A2ATaskEventSink"),
    "A2ATaskEventOutbox": ("ksadk.a2a.task_event_outbox", "A2ATaskEventOutbox"),
    "A2ATransportLease": ("ksadk.a2a.external_transport", "A2ATransportLease"),
    "A2A_TOOL_ACTIVITY_EXTENSION_URI": (
        "ksadk.a2a.presentation",
        "A2A_TOOL_ACTIVITY_EXTENSION_URI",
    ),
    "A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES": (
        "ksadk.a2a.presentation",
        "A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES",
    ),
    "A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES": (
        "ksadk.a2a.presentation",
        "A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES",
    ),
    "A2A_TOOL_ACTIVITY_SCHEMA": ("ksadk.a2a.presentation", "A2A_TOOL_ACTIVITY_SCHEMA"),
    "A2ATrustedIdentityResolver": ("ksadk.a2a.identity", "A2ATrustedIdentityResolver"),
    "AgentEngineA2ABootstrap": ("ksadk.a2a.bootstrap", "AgentEngineA2ABootstrap"),
    "CallableA2ARouteOpener": ("ksadk.a2a.external_transport", "CallableA2ARouteOpener"),
    "CallableGatewayIdentityVerifier": ("ksadk.a2a.identity", "CallableGatewayIdentityVerifier"),
    "CallableGatewayProbeVerifier": ("ksadk.a2a.identity", "CallableGatewayProbeVerifier"),
    "CredentialInjection": ("ksadk.a2a.control_plane", "CredentialInjection"),
    "DiscoveredAgent": ("ksadk.a2a.space_client", "DiscoveredAgent"),
    "ERR_VPC_EGRESS_DIALER_REQUIRED": (
        "ksadk.a2a.external_transport",
        "ERR_VPC_EGRESS_DIALER_REQUIRED",
    ),
    "DEFAULT_A2A_EVENT_OUTBOX_PATH": (
        "ksadk.a2a.task_event_outbox",
        "DEFAULT_A2A_EVENT_OUTBOX_PATH",
    ),
    "ENV_A2A_CONTROL_PLANE_URL": ("ksadk.a2a.control_plane", "ENV_A2A_CONTROL_PLANE_URL"),
    "ENV_A2A_ENABLE_PUBLIC_EGRESS": ("ksadk.a2a.space_client", "ENV_A2A_ENABLE_PUBLIC_EGRESS"),
    "ENV_A2A_EVENT_OUTBOX_PATH": ("ksadk.a2a.task_event_outbox", "ENV_A2A_EVENT_OUTBOX_PATH"),
    "ENV_A2A_SPACE_ID": ("ksadk.a2a.space_client", "ENV_A2A_SPACE_ID"),
    "ENV_A2A_SPACE_IDS": ("ksadk.a2a.space_client", "ENV_A2A_SPACE_IDS"),
    "ENV_A2A_TOKEN_DIR": ("ksadk.a2a.control_plane", "ENV_A2A_TOKEN_DIR"),
    "ERR_PUBLIC_EGRESS_DISABLED": ("ksadk.a2a.space_client", "ERR_PUBLIC_EGRESS_DISABLED"),
    "EXECUTION_SCOPE_SCHEMA": ("ksadk.a2a.presentation", "EXECUTION_SCOPE_SCHEMA"),
    "FileWorkloadTokenProvider": ("ksadk.a2a.control_plane", "FileWorkloadTokenProvider"),
    "GatewayIdentityVerifier": ("ksadk.a2a.identity", "GatewayIdentityVerifier"),
    "GatewayProbeVerifier": ("ksadk.a2a.identity", "GatewayProbeVerifier"),
    "GuardedA2AExternalTransport": ("ksadk.a2a.external_transport", "GuardedA2AExternalTransport"),
    "InMemoryA2ATaskEventOutbox": ("ksadk.a2a.task_event_outbox", "InMemoryA2ATaskEventOutbox"),
    "InMemoryA2AResumeStateStore": ("ksadk.a2a.resume_store", "InMemoryA2AResumeStateStore"),
    "JSONRPC_PATH": ("ksadk.a2a.card", "JSONRPC_PATH"),
    "InternalA2AControlPlaneClient": ("ksadk.a2a.control_plane", "InternalA2AControlPlaneClient"),
    "PreparedA2AOperation": ("ksadk.a2a.control_plane", "PreparedA2AOperation"),
    "PRESENTATION_INPUT_CAPABILITY": ("ksadk.a2a.presentation", "PRESENTATION_INPUT_CAPABILITY"),
    "PRESENTATION_OUTPUT_CAPABILITY": ("ksadk.a2a.presentation", "PRESENTATION_OUTPUT_CAPABILITY"),
    "RemoteTaskReference": ("ksadk.a2a.control_plane", "RemoteTaskReference"),
    "RemoteAgentSpec": ("ksadk.a2a.invocation_types", "RemoteAgentSpec"),
    "REST_PATH_PREFIX": ("ksadk.a2a.card", "REST_PATH_PREFIX"),
    "SpaceAgentPage": ("ksadk.a2a.space_client", "SpaceAgentPage"),
    "SQLiteA2ATaskEventOutbox": ("ksadk.a2a.task_event_outbox", "SQLiteA2ATaskEventOutbox"),
    "SQLiteA2AContextStore": ("ksadk.a2a.context_store", "SQLiteA2AContextStore"),
    "SQLiteA2AResumeStateStore": ("ksadk.a2a.resume_store", "SQLiteA2AResumeStateStore"),
    "RuntimeA2AMetadata": ("ksadk.a2a.bootstrap", "RuntimeA2AMetadata"),
    "RuntimeLocalA2AExternalTransport": (
        "ksadk.a2a.external_transport",
        "RuntimeLocalA2AExternalTransport",
    ),
    "WorkloadTokenProvider": ("ksadk.a2a.control_plane", "WorkloadTokenProvider"),
    "add_a2a_protocol_routes": ("ksadk.a2a.routes", "add_a2a_protocol_routes"),
    "build_a2a_task_store": ("ksadk.a2a.task_store", "build_a2a_task_store"),
    "build_a2a_internal_action_path": ("ksadk.a2a.control_plane", "build_a2a_internal_action_path"),
    "build_agent_card": ("ksadk.a2a.card", "build_agent_card"),
    "build_public_tool_activity": ("ksadk.a2a.presentation", "build_public_tool_activity"),
    "canonical_a2a_authn_mode": ("ksadk.a2a.identity", "canonical_a2a_authn_mode"),
    "ExecutionScopeDescriptor": ("ksadk.a2a.presentation", "ExecutionScopeDescriptor"),
    "negotiate_presentation_profile": ("ksadk.a2a.presentation", "negotiate_presentation_profile"),
    "stream_a2a_agent": ("ksadk.a2a.langgraph", "stream_a2a_agent"),
    "stream_a2a_agent_events": ("ksadk.a2a.langgraph", "stream_a2a_agent_events"),
    "stream_a2a_agent_to_writer": ("ksadk.a2a.langgraph", "stream_a2a_agent_to_writer"),
    "ToolActivity": ("ksadk.a2a.presentation", "ToolActivity"),
    "validate_scope_transition": ("ksadk.a2a.presentation", "validate_scope_transition"),
    "validate_tool_activity_batch": ("ksadk.a2a.presentation", "validate_tool_activity_batch"),
    "A2AInvocationService": ("ksadk.a2a.invocation_service", "A2AInvocationService"),
    "A2AInvocationTimeouts": ("ksadk.a2a.invocation_service", "A2AInvocationTimeouts"),
    "A2AInvocationPolicy": ("ksadk.a2a.invocation_policy", "A2AInvocationPolicy"),
    "A2AInvocationPolicyDecision": ("ksadk.a2a.invocation_policy", "A2AInvocationPolicyDecision"),
    "A2ARemoteAgent": ("ksadk.a2a.adk", "A2ARemoteAgent"),
    "bind_managed_a2a": ("ksadk.a2a.adk", "bind_managed_a2a"),
    "remote_agent": ("ksadk.a2a.adk", "remote_agent"),
    "PublicToolActivityPolicy": ("ksadk.a2a.tool_activity", "PublicToolActivityPolicy"),
    "PublicToolRule": ("ksadk.a2a.tool_activity", "PublicToolRule"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
