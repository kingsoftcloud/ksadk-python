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
"""

from ksadk.a2a.bootstrap import A2ACheckpointStore, AgentEngineA2ABootstrap, RuntimeA2AMetadata
from ksadk.a2a.card import (
    A2A_PROTOCOL_VERSION,
    JSONRPC_PATH,
    REST_PATH_PREFIX,
    build_agent_card,
)
from ksadk.a2a.context_store import (
    A2AContextIdentity,
    A2AContextStore,
    SQLiteA2AContextStore,
)
from ksadk.a2a.control_plane import (
    A2A_INTERNAL_ACTIONS,
    A2A_INTERNAL_PATH_PREFIX,
    ENV_A2A_CONTROL_PLANE_URL,
    ENV_A2A_TOKEN_DIR,
    A2AControlPlane,
    A2AControlPlaneError,
    A2AInternalAction,
    A2AOperation,
    A2ARoute,
    A2ARouteInterface,
    A2ATarget,
    CredentialInjection,
    FileWorkloadTokenProvider,
    InternalA2AControlPlaneClient,
    PreparedA2AOperation,
    RemoteTaskReference,
    WorkloadTokenProvider,
    build_a2a_internal_action_path,
)
from ksadk.a2a.event_adapter import A2AEventAdapter
from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.a2a.external_transport import (
    ERR_VPC_EGRESS_DIALER_REQUIRED,
    A2AExternalTransport,
    A2ARouteOpener,
    A2ATransportLease,
    CallableA2ARouteOpener,
    GuardedA2AExternalTransport,
    RuntimeLocalA2AExternalTransport,
)
from ksadk.a2a.identity import (
    A2AGatewayIdentityMiddleware,
    A2AIngressIdentity,
    A2AIngressTargetBinding,
    A2ATrustedIdentityResolver,
    CallableGatewayIdentityVerifier,
    CallableGatewayProbeVerifier,
    GatewayIdentityVerifier,
    GatewayProbeVerifier,
)
from ksadk.a2a.langgraph import (
    A2AStreamEvent,
    stream_a2a_agent,
    stream_a2a_agent_events,
    stream_a2a_agent_to_writer,
)
from ksadk.a2a.resume_store import (
    A2AResumePayloadKind,
    A2AResumeState,
    A2AResumeStateStore,
    InMemoryA2AResumeStateStore,
    SQLiteA2AResumeStateStore,
)
from ksadk.a2a.routes import A2AConfig, add_a2a_protocol_routes
from ksadk.a2a.server import A2AProtocolServer
from ksadk.a2a.space_client import (
    ENV_A2A_ENABLE_PUBLIC_EGRESS,
    ENV_A2A_SPACE_IDS,
    ERR_PUBLIC_EGRESS_DISABLED,
    A2APlatformTask,
    A2ASpaceClient,
    DiscoveredAgent,
    SpaceAgentPage,
)
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.a2a.task_event_dispatcher import A2ATaskEventDispatcher, A2ATaskEventSink
from ksadk.a2a.task_event_outbox import (
    DEFAULT_A2A_EVENT_OUTBOX_PATH,
    ENV_A2A_EVENT_OUTBOX_PATH,
    A2ATaskEventBatch,
    A2ATaskEventOutbox,
    InMemoryA2ATaskEventOutbox,
    SQLiteA2ATaskEventOutbox,
)
from ksadk.a2a.task_store import A2A_TASK_TABLE, build_a2a_task_store

__all__ = [
    "A2AConfig",
    "A2ACheckpointStore",
    "A2AContextIdentity",
    "A2AContextStore",
    "A2AControlPlane",
    "A2AControlPlaneError",
    "A2AInternalAction",
    "A2A_INTERNAL_ACTIONS",
    "A2A_INTERNAL_PATH_PREFIX",
    "A2AOperation",
    "A2AEventAdapter",
    "A2AExternalTransport",
    "A2AGatewayIdentityMiddleware",
    "A2AIngressIdentity",
    "A2AIngressTargetBinding",
    "A2APlatformTask",
    "A2AProtocolServer",
    "A2ARuntimeExecutor",
    "A2ARuntimeTaskAdapter",
    "A2AResumeState",
    "A2AResumePayloadKind",
    "A2AResumeStateStore",
    "A2ASpaceClient",
    "A2AStreamEvent",
    "A2A_PROTOCOL_VERSION",
    "A2ARoute",
    "A2ARouteOpener",
    "A2ARouteInterface",
    "A2A_TASK_TABLE",
    "A2ATarget",
    "A2ATaskEventBatch",
    "A2ATaskEventDispatcher",
    "A2ATaskEventSink",
    "A2ATaskEventOutbox",
    "A2ATransportLease",
    "A2ATrustedIdentityResolver",
    "AgentEngineA2ABootstrap",
    "CallableA2ARouteOpener",
    "CallableGatewayIdentityVerifier",
    "CallableGatewayProbeVerifier",
    "CredentialInjection",
    "DiscoveredAgent",
    "ERR_VPC_EGRESS_DIALER_REQUIRED",
    "DEFAULT_A2A_EVENT_OUTBOX_PATH",
    "ENV_A2A_CONTROL_PLANE_URL",
    "ENV_A2A_ENABLE_PUBLIC_EGRESS",
    "ENV_A2A_EVENT_OUTBOX_PATH",
    "ENV_A2A_SPACE_IDS",
    "ENV_A2A_TOKEN_DIR",
    "ERR_PUBLIC_EGRESS_DISABLED",
    "FileWorkloadTokenProvider",
    "GatewayIdentityVerifier",
    "GatewayProbeVerifier",
    "GuardedA2AExternalTransport",
    "InMemoryA2ATaskEventOutbox",
    "InMemoryA2AResumeStateStore",
    "JSONRPC_PATH",
    "InternalA2AControlPlaneClient",
    "PreparedA2AOperation",
    "RemoteTaskReference",
    "REST_PATH_PREFIX",
    "SpaceAgentPage",
    "SQLiteA2ATaskEventOutbox",
    "SQLiteA2AContextStore",
    "SQLiteA2AResumeStateStore",
    "RuntimeA2AMetadata",
    "RuntimeLocalA2AExternalTransport",
    "WorkloadTokenProvider",
    "add_a2a_protocol_routes",
    "build_a2a_task_store",
    "build_a2a_internal_action_path",
    "build_agent_card",
    "stream_a2a_agent",
    "stream_a2a_agent_events",
    "stream_a2a_agent_to_writer",
]
