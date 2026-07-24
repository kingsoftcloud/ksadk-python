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
与 HarnessApp 共用;A2A 是数据面 route group,不承载注册副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from a2a.server.routes import add_a2a_routes_to_fastapi
from a2a.server.tasks import TaskStore
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from ksadk.a2a.card import build_agent_card
from ksadk.a2a.server import A2AProtocolServer
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.a2a.task_store import A2A_TASK_TABLE, A2AOwnerContextBuilder, build_a2a_task_store


@dataclass
class A2AConfig:
    """A2A 协议装配配置(数据面 route group)。"""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8000"
    agent_name: str = "agent"
    description: str = ""
    version: str = "1.0.0"
    skills: Sequence[str] = field(default_factory=tuple)
    streaming: bool = True
    prefer_stream: bool = True
    # durable task store:dsn(如 sqlite+aiosqlite:///.ksadk_a2a_tasks.db 或
    # postgresql+asyncpg://...)或外部传入 engine/task_store。
    task_store_dsn: Optional[str] = None
    task_table: str = A2A_TASK_TABLE
    create_table: bool = True


def add_a2a_protocol_routes(
    app: FastAPI,
    runner: Any,
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
        runner,
        agent_card=card,
        task_store=store,
        task_adapter=task_adapter,
        context_builder=context_builder,
        prefer_stream=config.prefer_stream,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=server.agent_card_routes(),
        jsonrpc_routes=server.jsonrpc_routes(),
        rest_routes=server.rest_routes(),
    )
    return server


__all__ = ["A2AConfig", "add_a2a_protocol_routes"]
