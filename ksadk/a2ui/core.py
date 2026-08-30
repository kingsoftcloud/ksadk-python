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
import time
import uuid
from typing import Any, Optional

from ksadk.a2ui.models import (
    BASIC_CATALOG,
    ActionReceipt,
    PendingInteraction,
    Surface,
)
from ksadk.events.canonical import (
    ContentSnapshot,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import DataContent
from ksadk.events.identity import (
    stable_event_id,
    stable_item_id,
    stable_scope_id,
)
from ksadk.events.store import RuntimeEventStore

logger = logging.getLogger(__name__)


class A2UICore:
    """A2UI Agent-facing interface:display / request_input / submit_action。

    所有 A2UI 事件经 :class:`RuntimeEventStore` 持久化(**不另开通道、不经 A2A 绕过**),
    供 session 级订阅 / replay / 审计消费。

    Canonical 映射:surface = ``item_kind=data`` + ``source.protocol="a2ui"``;
    user action / input request = ``InteractionRequested``。
    """

    def __init__(
        self,
        store: RuntimeEventStore,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        catalog: dict[str, frozenset[str]] = BASIC_CATALOG,
        interaction_ledger: Any | None = None,
        interaction_guard: Any | None = None,
        tenant_id: str = "default",
    ) -> None:
        self._store = store
        self._agent_id = agent_id
        self._user_id = user_id
        self._session_id = session_id
        self._catalog = catalog
        self._seq = 0
        self._seen_surfaces: set[str] = set()
        # InteractionLedger 存在时它是 pending interaction 的唯一权威
        # （Phase 1 Task 5 Step 6）；本地 dict 只作无 ledger 的降级路径。
        self._ledger = interaction_ledger
        self._guard = interaction_guard
        self._tenant_id = tenant_id
        self._pending: dict[str, PendingInteraction] = {}

    # ---- 内部:canonical 身份/信封 ----

    def _scope_id(self, invocation_id: str) -> str:
        return stable_scope_id("ksadk", self._session_id, invocation_id)

    def _surface_item_id(self, invocation_id: str, surface_id: str) -> str:
        return stable_item_id("ksadk", self._session_id, invocation_id, "a2ui", surface_id)

    def _source(self, invocation_id: str, surface_id: str) -> SourceRef:
        return SourceRef(
            framework="ksadk",
            protocol="a2ui",
            native_run_id=invocation_id,
            metadata={
                "agent_id": self._agent_id,
                "user_id": self._user_id,
                "session_id": self._session_id,
                "invocation_id": invocation_id,
                "surface_id": surface_id,
            },
        )

    def _envelope(
        self,
        invocation_id: str,
        item_id: str,
        event_type: str,
        part_id: str,
        surface_id: str = "",
    ) -> dict[str, Any]:
        self._seq += 1
        return {
            "schema_version": 2,
            "event_id": stable_event_id(
                "ksadk", self._scope_id(invocation_id), item_id, event_type, part_id,
                invocation_id, self._seq,
            ),
            "seq": self._seq,
            "timestamp": time.time(),
            "run_id": invocation_id,
            "scope_id": self._scope_id(invocation_id),
            "source": self._source(invocation_id, surface_id or item_id.split(":")[-1]),
        }

    async def _append(self, event: RuntimeEvent) -> RuntimeEvent:
        await self._store.append_one(self._session_id, event)
        return event

    # ---- 三种交互 ----

    async def display_ui(
        self,
        surface: Surface,
        *,
        invocation_id: str,
        origin: str = "local",
    ) -> str:
        """展示 surface(不阻塞)。首显发 item.started,重复显发 item.updated。"""
        surface.validate(self._catalog)
        item_id = self._surface_item_id(invocation_id, surface.surface_id)
        part_id = "a2ui-surface"
        surface_data = surface.to_dict()
        is_new = surface.surface_id not in self._seen_surfaces
        self._seen_surfaces.add(surface.surface_id)
        if is_new:
            event = ItemStarted(
                **self._envelope(invocation_id, item_id, "item.started", part_id, surface_id=surface.surface_id),
                item_id=item_id,
                item_kind="data",
                initial=ContentSnapshot(
                    parts=(DataContent(part_id=part_id, data=surface_data),)
                ),
            )
        else:
            event = ItemUpdated(
                **self._envelope(invocation_id, item_id, "item.updated", part_id, surface_id=surface.surface_id),
                item_id=item_id,
                item_kind="data",
                op="replace",
                update=DataContent(part_id=part_id, data=surface_data),
            )
        await self._append(event)
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
        interaction_id = f"int_{uuid.uuid4().hex[:12]}"
        if self._ledger is not None and self._guard is not None:
            # Phase 1 Task 5 Step 6:durable ledger 是 pending interaction 的
            # 唯一权威;持久化身份与 interaction.requested 事实由 ledger 落盘。
            from datetime import datetime, timezone

            from ksadk.interaction.contracts import InteractionRecord

            record = InteractionRecord(
                interaction_id=interaction_id,
                tenant_id=self._tenant_id,
                agent_instance_id=self._agent_id,
                session_id=self._session_id,
                run_id=invocation_id,
                kind="structured_input",
                request_schema=dict(schema),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            stored = await self._ledger.request(record, guard=self._guard)
            interaction = PendingInteraction(
                interaction_id=stored.interaction_id,
                surface_id=surface.surface_id,
                kind=kind,
                input_schema=dict(schema),
                status=stored.status,
            )
            self._pending[interaction.interaction_id] = interaction
            # canonical A2UI wire 事实照常产出(durable 权威在 ledger)。
            item_id = stable_item_id(
                "ksadk", self._session_id, invocation_id,
                "a2ui-interaction", interaction.interaction_id,
            )
            from ksadk.events.canonical import StructuredInputRequest

            request = StructuredInputRequest(prompt=None, schema=dict(schema))
            event = InteractionRequested(
                **self._envelope(
                    invocation_id, item_id, "interaction.requested", "a2ui-interaction"
                ),
                interaction_id=interaction.interaction_id,
                interaction_kind="structured_input",
                request=request,
            )
            event.source.metadata["surface_id"] = surface.surface_id
            event.source.metadata["kind"] = kind
            await self._append(event)
            return interaction
        interaction = PendingInteraction(
            interaction_id=interaction_id,
            surface_id=surface.surface_id,
            kind=kind,
            input_schema=dict(schema),
        )
        self._pending[interaction.interaction_id] = interaction
        item_id = stable_item_id(
            "ksadk", self._session_id, invocation_id, "a2ui-interaction", interaction.interaction_id
        )
        from ksadk.events.canonical import StructuredInputRequest

        request = StructuredInputRequest(prompt=None, schema=dict(schema))
        event = InteractionRequested(
            **self._envelope(
                invocation_id, item_id, "interaction.requested", "a2ui-interaction"
            ),
            interaction_id=interaction.interaction_id,
            interaction_kind="structured_input",
            request=request,
        )
        event.source.metadata["surface_id"] = surface.surface_id
        event.source.metadata["kind"] = kind
        await self._append(event)
        return interaction

    async def submit_action(
        self,
        action: dict[str, Any],
        *,
        invocation_id: str,
        origin: str = "local",
    ) -> ActionReceipt:
        """非阻塞 action(原 run 可已结束):登记 action.received,返回幂等回执。

        ``action``: ``{"action_id","surface_id","name","actor"?,"component_id"?}``;
        携带 ``interaction_id`` 且配置了 InteractionLedger 时,对原 ID 建
        InteractionSubmission 走 durable resolve,不再发第二个
        InteractionRequested(Phase 1 Task 5 Step 6)。
        """
        receipt = ActionReceipt(
            action_id=str(action.get("action_id") or f"act_{uuid.uuid4().hex[:12]}"),
            surface_id=str(action["surface_id"]),
            name=str(action["name"]),
            actor=str(action.get("actor") or "user"),
            status="received",
        )
        if (
            self._ledger is not None
            and self._guard is not None
            and action.get("interaction_id")
        ):
            # durable 路径:对原 interaction 建 submission(first-wins),
            # 不再产出第二个 interaction.requested 事实。
            from ksadk.interaction.contracts import InteractionSubmission

            submission = InteractionSubmission(
                interaction_id=str(action["interaction_id"]),
                expected_revision=int(action.get("expected_revision") or 1),
                action="submit",
                response=dict(action),
                idempotency_key=f"a2ui-action:{receipt.action_id}",
            )
            resolved = await self._ledger.resolve(submission, guard=self._guard)
            receipt.status = resolved.status
            pending = self._pending.get(submission.interaction_id)
            if pending is not None:
                pending.status = resolved.status
            return receipt
        item_id = stable_item_id(
            "ksadk", self._session_id, invocation_id, "a2ui-action", receipt.action_id
        )
        from ksadk.events.canonical import ApprovalRequest

        request = ApprovalRequest(
            call_id=None,
            kind="a2ui_action",
            detail={
                "action_id": receipt.action_id,
                "surface_id": receipt.surface_id,
                "name": receipt.name,
                "actor": receipt.actor,
                "component_id": action.get("component_id"),
                "origin": origin,
            },
        )
        event = InteractionRequested(
            **self._envelope(
                invocation_id, item_id, "interaction.requested", "a2ui-action"
            ),
            interaction_id=receipt.action_id,
            interaction_kind="approval",
            request=request,
        )
        await self._append(event)
        return receipt

    async def end_surface(self, surface_id: str, *, invocation_id: str) -> None:
        """结束 surface(item.completed)。"""
        item_id = self._surface_item_id(invocation_id, surface_id)
        event = ItemCompleted(
            **self._envelope(invocation_id, item_id, "item.completed", "a2ui-surface", surface_id=surface_id),
            item_id=item_id,
            item_kind="data",
            snapshot=ContentSnapshot(parts=()),
        )
        await self._append(event)
        self._seen_surfaces.discard(surface_id)

    # ---- 查询 ----

    def pending_interaction(self, interaction_id: str) -> Optional[PendingInteraction]:
        """查询 pending interaction。

        配置了 ledger 时,durable 台账是权威;本地缓存条目由
        request_ui_input / submit_action 随 durable 事实同步更新。
        """
        return self._pending.get(interaction_id)

    async def pending_interaction_record(self, interaction_id: str):
        """durable 视角:从 InteractionLedger 读取 InteractionRecord。"""
        if self._ledger is None:
            return None
        return await self._ledger.get(interaction_id)


__all__ = ["A2UICore"]
