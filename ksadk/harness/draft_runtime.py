"""Draft Runtime 调试通路（P2/P2.1「保存即可试用」）。

不改变 KsADK 正式生命周期（Revision → Build → Approval → Deploy →
Activate），增加一条调试路径::

    创建或修改 Draft → 保存时完成 Runtime 编译 → 立即打开测试对话

P2.1 收口：

- **DraftConversation 稳定多轮会话**：同一草稿内多轮测试对话共享上下文
  （历史经 ``conversation_history`` 注入，与 Studio Playground 同一通路）；
- **审批可恢复**：MCP 高风险调用进入 awaiting_approval 时保留 Handle，
  ``approve()/reject()`` 经引擎 resume 通道续跑，草稿内审批照常工作；
- **保存时完成全部 Runtime 校验**：``compile()`` 异步完成
  ``engine.compile(spec)``（工具名冲突、required MCP 无 Runtime 等编译期
  校验在保存时即暴露，不拖到首次发消息）；
- **TTL / 关闭 / 替换清理**：会话惰性过期、显式关闭、同 draft_id 重新
  保存自动替换旧会话。

P2.2 补强连续审批、恢复失败重试、TTL 挂起句柄确定性释放，并与
ContextEngine 的 current input 去重约定对齐。

隔离不变：draft 引用命名空间（``agent-revision://draft-<id>@1``）、
``draft:`` 会话前缀、metadata 标记、正式生命周期零感知。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from ksadk.harness.compiler import compile_revision_payload
from ksadk.harness.spec import HarnessSpec

#: Draft Revision 引用前缀——合法 agent-revision 引用内的草稿命名空间
#: （``agent-revision://draft-<id>@1``），与正式 Revision 隔离但不破坏引用校验。
DRAFT_REF_PREFIX = "agent-revision://draft-"

#: Draft 会话前缀——正式对话永不落入该命名空间。
DRAFT_SESSION_PREFIX = "draft:"

#: 默认会话 TTL（秒）：超时未活动的 Draft 会话被惰性清理。
DEFAULT_SESSION_TTL_SECONDS = 3600.0


class DraftRuntimeError(RuntimeError):
    """Draft 编译失败或非法操作。"""


@dataclass
class _PendingApproval:
    """awaiting_approval 的挂起 Run（Handle 保留，等待决策）。"""

    handle: Any
    call_id: str


class DraftConversation:
    """一个草稿的稳定多轮测试会话。"""

    def __init__(self, draft_id: str, spec: HarnessSpec, engine: Any,
                 warnings: tuple[str, ...] = ()) -> None:
        self.draft_id = draft_id
        self.spec = spec
        self.engine = engine
        self.warnings = warnings
        self.turns = 0
        #: 跨轮共享的对话历史（role/content 投影）。
        self.history: list[dict[str, str]] = []
        #: 挂起的审批（awaiting_approval 时非空）。
        self.pending: _PendingApproval | None = None
        self._compiled: Any | None = None
        self._session_id = f"{DRAFT_SESSION_PREFIX}{draft_id}"
        self.last_active: float = time.monotonic()

    @property
    def draft_ref(self) -> str:
        return f"{DRAFT_REF_PREFIX}{self.draft_id}@1"

    @property
    def awaiting_approval(self) -> bool:
        return self.pending is not None

    # ------------------------------------------------------------- 对话

    async def converse(self, message: str, *, user_id: str = "draft-user") -> list[Any]:
        """一轮测试对话。多轮共享上下文；有挂起审批时必须先决策。"""
        if self.pending is not None:
            raise DraftRuntimeError(
                "存在挂起审批，先调用 approve()/reject() 或 discard_approval()"
            )
        handle = await self._start_run(message, user_id=user_id)
        events = await self._drain(handle)
        return events

    # ------------------------------------------------------------- 审批

    async def approve(self) -> list[Any]:
        """批准挂起的高风险调用并续跑。"""
        return await self._resolve_approval("approved")

    async def reject(self) -> list[Any]:
        """拒绝挂起的高风险调用并续跑（工具收到拒绝，Run 正常收尾）。"""
        return await self._resolve_approval("rejected")

    async def discard_approval(self) -> None:
        """放弃挂起的 Run（不决策，直接丢弃）。"""
        if self.pending is None:
            raise DraftRuntimeError("没有挂起的审批")
        await self.engine.close(self.pending.handle)
        self.pending = None

    # ------------------------------------------------------------- 关闭

    async def close(self) -> None:
        """即用即弃：释放挂起 Handle（正常轮次已逐轮关闭）。"""
        if self.pending is not None:
            await self.engine.close(self.pending.handle)
            self.pending = None

    # ------------------------------------------------------------ 内部

    async def ensure_compiled(self) -> None:
        if self._compiled is None:
            self._compiled = await self.engine.compile(self.spec)

    async def _start_run(self, message: str, *, user_id: str) -> Any:
        from ksadk.runtime import StartRequest

        await self.ensure_compiled()
        # 历史含本轮输入（与 evaluation/Playground 的注入约定一致）。
        history = list(self.history)
        history.append({"role": "user", "content": str(message)})
        self.turns += 1
        self.last_active = time.monotonic()
        return await self.engine.start(
            StartRequest(
                agent_id=f"draft-{self.draft_id}",
                user_id=user_id,
                session_id=self._session_id,
                input=message,
                metadata={
                    "draft": True,
                    "draft_id": self.draft_id,
                    "conversation_history": history,
                },
            ),
            self._compiled,
        )

    async def _drain(self, handle: Any) -> list[Any]:
        events = [event async for event in self.engine.stream(handle)]
        from ksadk.harness.events import EventType

        approval = next(
            (
                e for e in reversed(events)
                if e.event_type == EventType.APPROVAL_REQUESTED
            ),
            None,
        )
        if approval is not None:
            # 审批挂起：保留 Handle 等决策，历史不推进。
            self.pending = _PendingApproval(
                handle=handle,
                call_id=str(approval.payload.get("call_id") or ""),
            )
            return events
        self._absorb(handle)
        await self.engine.close(handle)
        return events

    async def _resolve_approval(self, decision: str) -> list[Any]:
        if self.pending is None:
            raise DraftRuntimeError("没有挂起的审批")
        from ksadk.runtime import ResumePayload, ResumeTarget

        pending = self.pending
        handle = pending.handle
        call_id = pending.call_id
        self.last_active = time.monotonic()
        try:
            await self.engine.resume(
                handle,
                ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
                ResumePayload(kind="approval_decision", call_id=call_id, data=decision),
            )
        except Exception:
            # Resume 失败时保留 pending，调用方可重试或显式放弃；
            # 不得在引擎拒绝恢复时丢失审批决策入口。
            self.pending = pending
            raise
        self.pending = None
        # 续跑后可能再次命中高风险工具；统一回到 _drain，
        # 使同一 Run 的连续审批仍保留 Handle，而不是误关闭。
        return await self._drain(handle)

    def _absorb(self, handle: Any) -> None:
        """把本轮完整对话投影并入跨轮历史。"""
        run = self.engine._runs.get(handle.run_id)
        if run is None:
            return
        self.history = [
            {"role": m.role.value, "content": str(m.content)}
            for m in run.state.messages
        ]


class DraftRuntime:
    """Draft → 保存时编译 → 测试对话的编排入口（生命周期外调试通路）。"""

    def __init__(
        self,
        *,
        reasoner: Any | None = None,
        local_dir: str | None = None,
        cache_dir: str | None = None,
        checkpointer: Any | None = None,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        **engine_kwargs: Any,
    ) -> None:
        self._reasoner = reasoner
        self._local_dir = local_dir
        self._cache_dir = cache_dir
        self._checkpointer = checkpointer
        self._session_ttl_seconds = session_ttl_seconds
        self._engine_kwargs = engine_kwargs
        self._sessions: dict[str, DraftConversation] = {}

    # ------------------------------------------------------------- 编译

    async def compile(
        self,
        draft_payload: dict[str, Any],
        *,
        draft_id: str | None = None,
    ) -> DraftConversation:
        """Draft payload → HarnessSpec → **保存时即完成 Runtime 编译**。

        校验三段全走正式路径：compile_revision_payload（payload 校验）→
        engine 装配（Skill 解析/MCP 绑定）→ engine.compile(spec)（工具名
        冲突、required MCP 无 Runtime 等运行时校验）。失败抛
        DraftRuntimeError——Studio 保存时即可暴露全部问题。
        """
        await self._expire_stale()
        draft_id = draft_id or f"draft_{uuid.uuid4().hex[:12]}"
        revision_ref = f"{DRAFT_REF_PREFIX}{draft_id}@1"
        try:
            spec = compile_revision_payload(draft_payload, revision_ref=revision_ref)
        except Exception as exc:  # noqa: BLE001
            raise DraftRuntimeError(f"draft 编译失败: {exc}") from exc
        engine, warnings = self._compose(spec)
        session = DraftConversation(draft_id, spec, engine, warnings=tuple(warnings))
        try:
            await session.ensure_compiled()
        except Exception as exc:  # noqa: BLE001
            raise DraftRuntimeError(f"draft runtime 校验失败: {exc}") from exc
        # 同 draft_id 重新保存：替换旧会话（旧挂起 Handle 先释放）。
        old = self._sessions.get(draft_id)
        if old is not None:
            await old.close()
        self._sessions[draft_id] = session
        return session

    async def session(self, draft_id: str) -> DraftConversation:
        """返回未过期的 Draft 会话，并确定性关闭所有过期句柄。"""
        await self._expire_stale()
        if draft_id not in self._sessions:
            raise DraftRuntimeError(f"unknown draft: {draft_id!r}")
        return self._sessions[draft_id]

    # ------------------------------------------------------------- 清理

    async def close_session(self, draft_id: str) -> None:
        session = self._sessions.pop(draft_id, None)
        if session is not None:
            await session.close()

    async def close(self) -> None:
        """销毁全部 Draft 会话（Studio 关闭调试面板时调用）。"""
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()

    async def _expire_stale(self) -> None:
        """TTL 惰性清理：先移出索引，再确定性关闭挂起 Handle。"""
        now = time.monotonic()
        stale: list[DraftConversation] = []
        for draft_id in list(self._sessions):
            session = self._sessions[draft_id]
            if now - session.last_active > self._session_ttl_seconds:
                self._sessions.pop(draft_id, None)
                stale.append(session)
        for session in stale:
            await session.close()

    # ------------------------------------------------------------ 内部

    def _compose(self, spec: HarnessSpec) -> tuple[Any, list[str]]:
        from ksadk.harness.skill_composition import compose_engine

        engine = compose_engine(
            spec,
            reasoner=self._reasoner,
            local_dir=self._local_dir,
            cache_dir=self._cache_dir,
            checkpointer=self._checkpointer,
            **self._engine_kwargs,
        )
        return engine, list(getattr(engine, "skill_warnings", ()))


__all__ = [
    "DEFAULT_SESSION_TTL_SECONDS",
    "DRAFT_REF_PREFIX",
    "DRAFT_SESSION_PREFIX",
    "DraftConversation",
    "DraftRuntime",
    "DraftRuntimeError",
]
