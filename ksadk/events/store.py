"""RuntimeEventStore — 统一事件 store + 两类订阅 + projection (goal-10,H2 §4.3)。

复用现有 ``SessionEvent(seq_id cursor)`` 持久化骨架(session service),**不另造存储、
不改表**:RuntimeEvent 的 ``phase``/``payload``/``schema_version``/``user_id`` 打包进
``SessionEvent.content``/``metadata``(均为自由 dict),并以 ``_RUNTIME_MARKER`` 标记区分
legacy session 事件(assistant_message/run_status 等),读取时只还原 runtime 事件。

- ``append`` / ``list``:RuntimeEvent ↔ SessionEvent 双向映射。
- ``subscribe_run``:单 invocation,终态(completed/failed/canceled)后关闭(对齐现有
  run 级 SSE 语义,新 schema)。
- ``subscribe_session``:session 级 cursor stream,跨 invocation,支持 run 后 action 与
  replay(A2UI 依赖)。
- ``project``:replay / 增量 projection(fold)。
- cursor 断线续传:订阅方持 ``last_seq_id``,断线后按 ``after_seq_id`` 重连,不丢不重。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable, Iterable, Optional

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.sessions.base import SessionEvent

logger = logging.getLogger(__name__)

#: SessionEvent.metadata 中的标记:该条是由 RuntimeEvent 持久化来的(区分 legacy 事件)。
_RUNTIME_MARKER = "ksadk_runtime_event"
_A2A_TASK_AGENT_STATE_KEY = "__ksadk_a2a_task_agents"

#: run 终态(subscribe_run 遇到即关闭;interrupted 是 input-required 暂停,非终态)。
_RUN_TERMINAL_EVENT_TYPES = frozenset(
    {
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELED,
    }
)

#: 默认订阅轮询间隔(秒)与单条流上限(秒,防泄漏)。
_DEFAULT_POLL_INTERVAL = 0.25
_DEFAULT_STREAM_TIMEOUT = 5 * 60


# ---------------------------------------------------------------------------
# RuntimeEvent ↔ SessionEvent 映射
# ---------------------------------------------------------------------------


def runtime_event_to_session_event(event: RuntimeEvent) -> SessionEvent:
    """把 RuntimeEvent 打包为 SessionEvent(content/metadata 承载新 schema 字段,不改表)。"""
    event.validate_conformance()
    return SessionEvent(
        id=event.event_id,
        session_id=event.session_id,
        author=event.agent_id,
        event_type=event.event_type,
        content={"phase": event.phase, "payload": dict(event.payload)},
        timestamp=event.timestamp,
        seq_id=event.seq_id,
        invocation_id=event.invocation_id,
        metadata={
            _RUNTIME_MARKER: True,
            "user_id": event.user_id,
            "schema_version": event.schema_version,
        },
    )


def session_event_to_runtime_event(event: SessionEvent) -> Optional[RuntimeEvent]:
    """把 SessionEvent 还原为 RuntimeEvent;非 runtime 事件(无标记)返回 None。"""
    if not (event.metadata or {}).get(_RUNTIME_MARKER):
        return None
    content = event.content or {}
    return RuntimeEvent.create(
        event.event_type,
        agent_id=event.author,
        user_id=str(event.metadata.get("user_id") or ""),
        session_id=event.session_id,
        invocation_id=event.invocation_id or "",
        seq_id=event.seq_id,
        payload=dict(content.get("payload") or {}),
        phase=content.get("phase"),
        event_id=event.id,
        timestamp=event.timestamp,
    )


# ---------------------------------------------------------------------------
# RuntimeEventStore
# ---------------------------------------------------------------------------


class RuntimeEventStore:
    """统一事件 store(复用 session service 的 seq_id cursor 持久化骨架)。"""

    def __init__(self, session_service: Any) -> None:
        self._service = session_service

    # ---- append ----

    async def append(self, events: Iterable[RuntimeEvent]) -> list[RuntimeEvent]:
        """持久化一组 RuntimeEvent,返回存储层分配 cursor 后的事件。"""
        appended: list[RuntimeEvent] = []
        for event in events:
            appended.append(await self.append_one(event))
        return appended

    async def append_one(self, event: RuntimeEvent) -> RuntimeEvent:
        """Idempotently persist one event using its durable ``event_id``.

        Replayed wire events return their original store-assigned cursor.  Reuse
        of an ID for different content is rejected rather than silently losing a
        legal event.
        """
        persisted, _created = await self.reserve_once(event)
        return persisted

    async def reserve_once(self, event: RuntimeEvent) -> tuple[RuntimeEvent, bool]:
        """Durably claim ``event.event_id`` and report whether this caller won.

        SQL backends enforce a unique event id, so this is also the command
        reservation seam for side effects such as checkpoint resume.  A loser
        receives the existing identical fact with ``created=False`` and must
        not repeat the side effect.
        """
        existing = await self._event_by_id(event.session_id, event.event_id)
        if existing is not None:
            self._assert_same_event(existing, event)
            return existing, False
        try:
            stored = await self._service.append_event(
                event.session_id, runtime_event_to_session_event(event)
            )
        except Exception:
            # Durable backends enforce a unique event id.  A concurrent writer
            # may win between the read and append; resolve that race by reading
            # the persisted fact and validating its content.
            existing = await self._event_by_id(event.session_id, event.event_id)
            if existing is None:
                raise
            self._assert_same_event(existing, event)
            return existing, False
        persisted = session_event_to_runtime_event(stored)
        if persisted is None:  # pragma: no cover - marker is set above by construction
            raise RuntimeError("RuntimeEvent 持久化后缺少 runtime marker")
        return persisted, True

    async def _event_by_id(self, session_id: str, event_id: str) -> RuntimeEvent | None:
        raw = await self._service.get_events(session_id)
        for stored in raw:
            if stored.id != event_id:
                continue
            return session_event_to_runtime_event(stored)
        return None

    @staticmethod
    def _assert_same_event(existing: RuntimeEvent, candidate: RuntimeEvent) -> None:
        comparable = (
            "event_type",
            "agent_id",
            "user_id",
            "session_id",
            "invocation_id",
            "phase",
            "payload",
        )
        if any(getattr(existing, field) != getattr(candidate, field) for field in comparable):
            raise ValueError(f"RuntimeEvent id collision for {candidate.event_id!r}")

    async def set_task_agent(self, session_id: str, task_id: str, agent_id: str) -> None:
        """Persist the outbound A2A task locator in session state."""
        session = await self._service.get_session_metadata(session_id)
        if session is None:
            raise ValueError(f"A2A space session {session_id!r} not found")
        current = await self._service.get_state(
            session.agent_id,
            session.user_id,
            session.id,
            scope="session",
        )
        mapping = dict((current.state if current else {}).get(_A2A_TASK_AGENT_STATE_KEY) or {})
        existing = mapping.get(task_id)
        if existing and existing != agent_id:
            raise ValueError(f"A2A task {task_id!r} is already bound to another agent")
        mapping[task_id] = agent_id
        await self._service.update_state(
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            scope="session",
            state_delta={_A2A_TASK_AGENT_STATE_KEY: mapping},
        )

    async def get_task_agent(self, session_id: str, task_id: str) -> str | None:
        """Resolve a persisted outbound A2A task locator."""
        session = await self._service.get_session_metadata(session_id)
        if session is None:
            return None
        current = await self._service.get_state(
            session.agent_id,
            session.user_id,
            session.id,
            scope="session",
        )
        mapping = dict((current.state if current else {}).get(_A2A_TASK_AGENT_STATE_KEY) or {})
        value = mapping.get(task_id)
        return str(value) if value else None

    # ---- list ----

    async def list(
        self,
        session_id: str,
        *,
        after_seq_id: int = 0,
        before_seq_id: Optional[int] = None,
        invocation_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[RuntimeEvent]:
        """按 seq cursor 读 RuntimeEvent(升序;可按 invocation 过滤 / before 上界回放)。"""
        raw = await self._service.get_events(
            session_id,
            after_seq_id=after_seq_id,
            before_seq_id=before_seq_id,
            limit=limit,
        )
        events = [e for e in (session_event_to_runtime_event(se) for se in raw) if e is not None]
        if invocation_id is not None:
            events = [e for e in events if e.invocation_id == invocation_id]
        events.sort(key=lambda e: e.seq_id)
        return events

    # ---- 两类订阅 ----

    async def subscribe_run(
        self,
        session_id: str,
        invocation_id: str,
        *,
        after_seq_id: int = 0,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        timeout: float = _DEFAULT_STREAM_TIMEOUT,
    ) -> AsyncIterator[RuntimeEvent]:
        """单 invocation 订阅:只产该 invocation 的 RuntimeEvent,终态后关闭。

        断线续传:调用方持返回事件的 ``seq_id``,断线后以 ``after_seq_id`` 重连即可续传,
        不丢(>after 的全部重发)、不重(<=after 的不重发)。
        """
        last = int(after_seq_id or 0)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            events = await self.list(session_id, after_seq_id=last, invocation_id=invocation_id)
            for event in events:
                last = max(last, event.seq_id)
                yield event
                if event.event_type in _RUN_TERMINAL_EVENT_TYPES:
                    return
            if asyncio.get_event_loop().time() > deadline:
                return
            await asyncio.sleep(poll_interval)

    async def subscribe_session(
        self,
        session_id: str,
        *,
        after_seq_id: int = 0,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        timeout: float = _DEFAULT_STREAM_TIMEOUT,
    ) -> AsyncIterator[RuntimeEvent]:
        """session 级 cursor stream:跨 invocation 产全部 RuntimeEvent(replay + live)。

        支持 run 后 action 与跨 invocation replay(A2UI 依赖);断线续传同 subscribe_run。
        """
        last = int(after_seq_id or 0)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            events = await self.list(session_id, after_seq_id=last)
            for event in events:
                last = max(last, event.seq_id)
                yield event
            if asyncio.get_event_loop().time() > deadline:
                return
            await asyncio.sleep(poll_interval)

    # ---- projection / replay ----

    async def project(
        self,
        session_id: str,
        projection: Optional[Callable[[Any, RuntimeEvent], Any]] = None,
        *,
        initial: Any = None,
        after_seq_id: int = 0,
        before_seq_id: Optional[int] = None,
    ) -> Any:
        """replay / projection。

        默认(``projection=None``):返回按 seq 升序的 RuntimeEvent 序列(replay)。
        给定 ``projection(acc, event) -> acc``:自 ``initial`` 起 fold 全部事件,支持
        增量 projection(以 ``after_seq_id`` 从某个 checkpoint 续投影)。
        """
        events = await self.list(session_id, after_seq_id=after_seq_id, before_seq_id=before_seq_id)
        if projection is None:
            return events
        acc = initial
        for event in events:
            acc = projection(acc, event)
        return acc


__all__ = [
    "RuntimeEventStore",
    "runtime_event_to_session_event",
    "session_event_to_runtime_event",
]
