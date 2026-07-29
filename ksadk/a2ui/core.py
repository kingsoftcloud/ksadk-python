"""A2UICore — Agent-facing interface + RuntimeEvent 产出 (goal-13,canonical §5.1/§5.2)。

**硬约束(canonical §1 / H2 P0-C):A2UI 必须先进入 RuntimeEvent,不得以 A2A executor
绕过**——本类的 ``display_ui`` / ``request_ui_input`` / ``submit_action`` 全部经 A7
``RuntimeEventStore`` 产出并持久化 A2UI 事件(``a2ui.surface.*`` / ``a2ui.interaction`` /
``a2ui.action``),由 session 级订阅承载(A2UI 依赖,可 run 后非阻塞 action 与回放)。

冻结 schema 映射(G0.2 已冻结,不改):surface 首显 ``a2ui.surface.begin`` / 更新
``a2ui.surface.update`` / 结束 ``a2ui.surface.end``;请求输入 ``a2ui.interaction``;
action ``a2ui.action``。均带 ``surface_id``(G0.2 必填键)。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from ksadk.a2ui.models import (
    BASIC_CATALOG,
    ActionReceipt,
    PendingInteraction,
    Surface,
)
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.events.store import RuntimeEventStore

logger = logging.getLogger(__name__)


class A2UICore:
    """A2UI Agent-facing interface:display / request_input / submit_action。

    所有 A2UI 事件经 :class:`RuntimeEventStore` 持久化(**不另开通道、不经 A2A 绕过**),
    供 session 级订阅 / replay / 审计消费。
    """

    def __init__(
        self,
        store: RuntimeEventStore,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        catalog: dict[str, frozenset[str]] = BASIC_CATALOG,
    ) -> None:
        self._store = store
        self._agent_id = agent_id
        self._user_id = user_id
        self._session_id = session_id
        self._catalog = catalog
        self._seq = 0
        self._seen_surfaces: set[str] = set()
        self._pending: dict[str, PendingInteraction] = {}

    # ---- 内部:产出并持久化一个 A2UI RuntimeEvent ----

    async def _emit(
        self, event_type: str, invocation_id: str, payload: dict[str, Any]
    ) -> RuntimeEvent:
        self._seq += 1
        event = RuntimeEvent.create(
            event_type,
            agent_id=self._agent_id,
            user_id=self._user_id,
            session_id=self._session_id,
            invocation_id=invocation_id,
            seq_id=self._seq,
            payload=payload,
        )
        # 经 RuntimeEvent 持久化(A7 store)——canonical,不绕过。
        await self._store.append_one(event)
        return event

    # ---- 三种交互 ----

    async def display_ui(
        self,
        surface: Surface,
        *,
        invocation_id: str,
        origin: str = "local",
    ) -> str:
        """展示 surface(不阻塞)。首显发 surface.begin,重复显发 surface.update。"""
        surface.validate(self._catalog)
        event_type = (
            EventType.A2UI_SURFACE_BEGIN
            if surface.surface_id not in self._seen_surfaces
            else EventType.A2UI_SURFACE_UPDATE
        )
        self._seen_surfaces.add(surface.surface_id)
        await self._emit(
            event_type,
            invocation_id,
            {
                "surface_id": surface.surface_id,
                "catalog_id": surface.catalog_id,
                "surface": surface.to_dict(),
                "origin": origin,
            },
        )
        return surface.surface_id

    async def request_ui_input(
        self,
        surface: Surface,
        *,
        schema: dict[str, Any],
        kind: str,
        invocation_id: str,
        origin: str = "local",
    ) -> PendingInteraction:
        """等待用户输入(input_required 语义):先展示 surface,再登记 pending interaction。

        返回的 :class:`PendingInteraction` 是一次等待输入的持久化身份;用户上行
        ``SubmitA2UIAction`` 命中后 resume(由 RuntimeBridge/adapter 处理,不在本层)。
        """
        # 先展示(确保 surface 已渲染),再请求输入。
        await self.display_ui(surface, invocation_id=invocation_id, origin=origin)
        interaction = PendingInteraction(
            interaction_id=f"int_{uuid.uuid4().hex[:12]}",
            surface_id=surface.surface_id,
            kind=kind,
            input_schema=dict(schema),
        )
        self._pending[interaction.interaction_id] = interaction
        await self._emit(
            EventType.A2UI_INTERACTION,
            invocation_id,
            {
                "surface_id": surface.surface_id,
                "interaction_id": interaction.interaction_id,
                "kind": kind,
                "input_schema": dict(schema),
            },
        )
        return interaction

    async def submit_action(
        self,
        action: dict[str, Any],
        *,
        invocation_id: str,
        origin: str = "local",
    ) -> ActionReceipt:
        """非阻塞 action(原 run 可已结束):登记 action.received,返回幂等回执。

        ``action``: ``{"action_id","surface_id","name","actor"?,"component_id"?}``。
        """
        receipt = ActionReceipt(
            action_id=str(action.get("action_id") or f"act_{uuid.uuid4().hex[:12]}"),
            surface_id=str(action["surface_id"]),
            name=str(action["name"]),
            actor=str(action.get("actor") or "user"),
            status="received",
        )
        await self._emit(
            EventType.A2UI_ACTION,
            invocation_id,
            {
                "surface_id": receipt.surface_id,
                "action_id": receipt.action_id,
                "name": receipt.name,
                "actor": receipt.actor,
                "component_id": action.get("component_id"),
                "origin": origin,
            },
        )
        return receipt

    async def end_surface(self, surface_id: str, *, invocation_id: str) -> None:
        """结束 surface(surface.end)。"""
        await self._emit(
            EventType.A2UI_SURFACE_END,
            invocation_id,
            {"surface_id": surface_id},
        )
        self._seen_surfaces.discard(surface_id)

    # ---- 查询 ----

    def pending_interaction(self, interaction_id: str) -> Optional[PendingInteraction]:
        return self._pending.get(interaction_id)


__all__ = ["A2UICore"]
