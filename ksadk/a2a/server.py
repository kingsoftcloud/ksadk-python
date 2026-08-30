"""A2AProtocolServer — 托管 Agent 的 A2A 协议数据面 (goal-05)。

契约 §3.2:``A2AProtocolServer`` = latest SDK route factories + AgentCard +
request handler + durable task store。装配进 create_runtime_app(§8),
**不再是独立 Starlette app**(旧 demo 的 ``A2AStarletteApplication`` 已废弃)。

路由由 ``ksadk.a2a.routes.add_a2a_protocol_routes`` 统一装配,一份实现,
普通 runtime app 与 HarnessApp 共用。
"""

from __future__ import annotations

from typing import Any, cast

from a2a.server.agent_execution import RequestContext, SimpleRequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import TaskStore
from a2a.types import AgentCard, SendMessageRequest, Task

from ksadk.a2a.card import JSONRPC_PATH, REST_PATH_PREFIX
from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.a2a.task_store import A2AOwnerContextBuilder


class _DurableRequestContextBuilder(SimpleRequestContextBuilder):
    """Recover the persisted context when a follow-up only carries task_id."""

    def __init__(self, task_store: TaskStore) -> None:
        super().__init__(should_populate_referred_tasks=False, task_store=task_store)
        self._durable_task_store = task_store

    async def build(
        self,
        context: ServerCallContext,
        params: SendMessageRequest | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        if task_id and (task is None or not context_id):
            persisted = await self._durable_task_store.get(task_id, context)
            if persisted is not None:
                task = persisted
                context_id = context_id or persisted.context_id
        return await super().build(
            context=context,
            params=params,
            task_id=task_id,
            context_id=context_id,
            task=task,
        )


class A2AProtocolServer:
    """把一个 RuntimeAdapter 暴露为 A2A 协议数据面。

    参数:
        agent_card: 符合 wire 1.0 的 AgentCard(``ksadk.a2a.card.build_agent_card``)。
        task_store: durable ``DatabaseTaskStore``(``ksadk.a2a.task_store``)。
        task_adapter: 可选 ``A2ARuntimeTaskAdapter``(提供则 cancel 走 RuntimeAdapter.cancel)。
        include_reasoning: 是否把 reasoning 输出为 ``adk_thought`` artifact。
    """

    def __init__(
        self,
        *,
        agent_card: AgentCard,
        task_store: TaskStore,
        task_adapter: A2ARuntimeTaskAdapter,
        context_builder: A2AOwnerContextBuilder | None = None,
        include_reasoning: bool = False,
    ) -> None:
        self.agent_card = agent_card
        self.task_store = task_store
        self.task_adapter = task_adapter
        self.context_builder = context_builder or A2AOwnerContextBuilder()
        self.executor = A2ARuntimeExecutor(
            task_adapter=task_adapter,
            include_reasoning=include_reasoning,
        )
        self.request_handler = DefaultRequestHandler(
            agent_executor=self.executor,
            task_store=task_store,
            agent_card=agent_card,
            request_context_builder=_DurableRequestContextBuilder(task_store),
        )

    def agent_card_routes(self) -> list[Any]:
        return cast(list[Any], create_agent_card_routes(self.agent_card))

    def jsonrpc_routes(self) -> list[Any]:
        return cast(
            list[Any],
            create_jsonrpc_routes(
                self.request_handler,
                rpc_url=JSONRPC_PATH,
                context_builder=self.context_builder,
            ),
        )

    def rest_routes(self) -> list[Any]:
        return cast(
            list[Any],
            create_rest_routes(
                self.request_handler,
                path_prefix=REST_PATH_PREFIX,
                context_builder=self.context_builder,
            ),
        )


__all__ = ["A2AProtocolServer"]
