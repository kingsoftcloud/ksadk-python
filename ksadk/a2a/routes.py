"""A2A 协议路由装配 — create_runtime_app 的 A2A seam (goal-05 §8)。

契约 §8:

```python
def create_runtime_app(config):
    app = create_base_data_plane_app(config)
    if config.a2a.enabled:
        add_a2a_protocol_routes(app, config.a2a)
    return app
```

A2A route、TaskStore、task adapter 和 card 构造**只有一份实现**,普通 runtime app
与 HarnessApp 共用;A2A 是数据面 route group,不承载注册副作用。``A2AConfig``
保留给本地开发和协议一致性测试；AgentEngine 产品 Runtime 必须通过
``AgentEngineA2ABootstrap`` 装配 durable storage、trusted ingress 和 egress guard。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from a2a.server.routes import add_a2a_routes_to_fastapi
from a2a.server.tasks import TaskStore
from a2a.types import AgentSkill
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from ksadk.a2a.card import build_agent_card
from ksadk.a2a.server import A2AProtocolServer
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.a2a.task_store import A2A_TASK_TABLE, A2AOwnerContextBuilder, build_a2a_task_store


@dataclass
class A2AConfig:
    """本地/一致性测试使用的 A2A 协议装配配置(数据面 route group)。"""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8000"
    agent_name: str = "agent"
    description: str = ""
    version: str = "1.0.0"
    skills: Sequence[str | AgentSkill] = field(default_factory=tuple)
    streaming: bool = True
    prefer_stream: bool = True
    # 本地调试可显式开放 reasoning artifact；托管 Runtime 默认关闭，避免把
    # 模型内部推理写入公开 A2A Task。
    include_reasoning: bool = False
    # durable task store:dsn(如 sqlite+aiosqlite:///.agentengine/a2a_tasks.db 或
    # postgresql+asyncpg://...)或外部传入 engine/task_store。
    task_store_dsn: Optional[str] = None
    task_table: str = A2A_TASK_TABLE
    create_table: bool = True


def add_a2a_protocol_routes(
    app: FastAPI,
    config: A2AConfig,
    *,
    task_adapter: A2ARuntimeTaskAdapter,
    task_store: Optional[TaskStore] = None,
    engine: Optional[AsyncEngine] = None,
    context_builder: A2AOwnerContextBuilder | None = None,
) -> A2AProtocolServer:
    """把 A2A 协议路由(JSONRPC + HTTP+JSON + AgentCard)装配到 app。

    返回构建好的 :class:`A2AProtocolServer`(便于调用方取 request_handler /
    task_store 做后续 subscribe/cancel 或测试)。
    """
    card = build_agent_card(
        name=config.agent_name,
        base_url=config.base_url,
        description=config.description,
        version=config.version,
        skills=config.skills,
        streaming=config.streaming,
    )
    store = task_store or build_a2a_task_store(
        dsn=config.task_store_dsn,
        engine=engine,
        table_name=config.task_table,
        create_table=config.create_table,
    )
    server = A2AProtocolServer(
        agent_card=card,
        task_store=store,
        task_adapter=task_adapter,
        context_builder=context_builder,
        include_reasoning=config.include_reasoning,
    )
    rest = server.rest_routes()
    # a2a-sdk REST routes 附带贪婪 Mount('/{tenant}')(多租户 catch-all),
    # 它会拦截 /v1/responses、/agentengine/api/v1/*、/chat 等单段前缀路径导致 404。
    # 只丢弃贪婪 Mount('/{tenant}')；保留其他 Mount（多租户显式路径如 /tenant-a/a2a/v1/*）。
    from starlette.routing import Mount as _Mount

    rest = [
        route for route in rest
        if not (isinstance(route, _Mount) and getattr(route, "path", "") == "/{tenant}")
    ]
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=server.agent_card_routes(),
        jsonrpc_routes=server.jsonrpc_routes(),
        rest_routes=rest,
    )
    return server


__all__ = ["A2AConfig", "add_a2a_protocol_routes"]
