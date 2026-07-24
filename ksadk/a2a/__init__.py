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

from ksadk.a2a.card import (
    A2A_PROTOCOL_VERSION,
    JSONRPC_PATH,
    REST_PATH_PREFIX,
    build_agent_card,
)
from ksadk.a2a.event_adapter import A2AEventAdapter
from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.a2a.routes import A2AConfig, add_a2a_protocol_routes
from ksadk.a2a.server import A2AProtocolServer
from ksadk.a2a.space_client import (
    ENV_A2A_ENABLE_PUBLIC_EGRESS,
    ENV_A2A_SERVICE_URL,
    ENV_A2A_SPACE_ID,
    ERR_REQUIRES_PUBLIC_EGRESS,
    A2ASpaceClient,
    DiscoveredAgent,
    KopSpaceDiscoveryBackend,
    SpaceAgentPage,
    SpaceDiscoveryBackend,
)
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.a2a.task_store import A2A_TASK_TABLE, build_a2a_task_store

__all__ = [
    "A2AConfig",
    "A2AEventAdapter",
    "A2AProtocolServer",
    "A2ARuntimeExecutor",
    "A2ARuntimeTaskAdapter",
    "A2ASpaceClient",
    "A2A_PROTOCOL_VERSION",
    "A2A_TASK_TABLE",
    "DiscoveredAgent",
    "ENV_A2A_ENABLE_PUBLIC_EGRESS",
    "ENV_A2A_SERVICE_URL",
    "ENV_A2A_SPACE_ID",
    "ERR_REQUIRES_PUBLIC_EGRESS",
    "JSONRPC_PATH",
    "KopSpaceDiscoveryBackend",
    "REST_PATH_PREFIX",
    "SpaceAgentPage",
    "SpaceDiscoveryBackend",
    "add_a2a_protocol_routes",
    "build_a2a_task_store",
    "build_agent_card",
]
