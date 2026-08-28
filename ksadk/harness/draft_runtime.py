"""Draft Runtime 调试通路（P2「保存即可试用」）。

不改变 KsADK 正式生命周期（Revision → Build → Approval → Deploy →
Activate），增加一条调试路径::

    创建或修改 Draft → 临时编译 Draft Runtime → 立即打开测试对话

关键性质：

- **编译等价**：Draft 用与正式 Build 完全相同的
  :func:`~ksadk.harness.compiler.compile_revision_payload` 校验与编译——
  Draft 能跑 ⇔ 正式能 Build，调试结论可迁移；
- **生命周期隔离**：不产生 BuildManifest 持久化、不注册本地 Route、
  不触碰 LocalLifecycleManager 的任何状态；正式路径零感知；
- **可观测隔离**：Draft Run 的 session 以 ``draft:`` 前缀命名空间隔离，
  metadata 携带 ``draft=True`` 与 ``draft_id``，事件流/审计可区分草稿与
  正式流量；
- **即用即弃**：DraftSession 关闭即销毁引擎与句柄，不留运行时残留。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ksadk.harness.compiler import compile_revision_payload
from ksadk.harness.spec import HarnessSpec

#: Draft Revision 引用前缀——合法 agent-revision 引用内的草稿命名空间
#: （``agent-revision://draft-<id>``），与正式 Revision 隔离但不破坏引用校验。
DRAFT_REF_PREFIX = "agent-revision://draft-"

#: Draft 会话前缀——正式对话永不落入该命名空间。
DRAFT_SESSION_PREFIX = "draft:"


class DraftRuntimeError(RuntimeError):
    """Draft 编译失败或非法操作。"""


@dataclass
class DraftSession:
    """一次「保存即可试用」的临时 Runtime 会话。"""

    draft_id: str
    spec: HarnessSpec
    engine: Any
    #: Skill 解析降级警告（可选 Skill 缺失等，不阻断测试对话）。
    warnings: tuple[str, ...] = ()
    #: 本会话已发起的测试对话次数。
    turns: int = 0
    _compiled: Any | None = field(default=None, repr=False)

    @property
    def draft_ref(self) -> str:
        return f"{DRAFT_REF_PREFIX}{self.draft_id}@1"

    async def converse(self, message: str, *, user_id: str = "draft-user") -> list[Any]:
        """立即打开一轮测试对话：start → stream → 收完整事件流。

        每次调用是独立的测试 Run（session 按 draft_id + 轮次隔离），
        不与正式对话或前一轮 Draft Run 共享线程。
        """
        from ksadk.runtime import StartRequest

        if self._compiled is None:
            self._compiled = await self.engine.compile(self.spec)
        session_id = f"{DRAFT_SESSION_PREFIX}{self.draft_id}:{self.turns + 1}"
        handle = await self.engine.start(
            StartRequest(
                agent_id=f"draft-{self.draft_id}",
                user_id=user_id,
                session_id=session_id,
                input=message,
                metadata={"draft": True, "draft_id": self.draft_id},
            ),
            self._compiled,
        )
        self.turns += 1
        events = [event async for event in self.engine.stream(handle)]
        await self.engine.close(handle)
        return events

    async def close(self) -> None:
        """即用即弃：测试对话逐轮即时关闭，无残留句柄，此为幂等空操作。"""
        return None


class DraftRuntime:
    """Draft → 临时编译 → 测试对话的编排入口（生命周期外调试通路）。"""

    def __init__(
        self,
        *,
        reasoner: Any | None = None,
        local_dir: str | None = None,
        cache_dir: str | None = None,
        **engine_kwargs: Any,
    ) -> None:
        self._reasoner = reasoner
        self._local_dir = local_dir
        self._cache_dir = cache_dir
        self._engine_kwargs = engine_kwargs
        self._sessions: dict[str, DraftSession] = {}

    # ------------------------------------------------------------- 编译

    def compile(
        self,
        draft_payload: dict[str, Any],
        *,
        draft_id: str | None = None,
    ) -> DraftSession:
        """Draft payload → HarnessSpec + 临时引擎。

        校验与编译走正式 compiler 同一条路（等价性保证）；失败抛
        DraftRuntimeError——在 Studio 保存时即可暴露问题，而非 Build 时。
        """
        draft_id = draft_id or f"draft_{uuid.uuid4().hex[:12]}"
        revision_ref = f"{DRAFT_REF_PREFIX}{draft_id}@1"
        try:
            spec = compile_revision_payload(draft_payload, revision_ref=revision_ref)
        except Exception as exc:  # noqa: BLE001
            raise DraftRuntimeError(f"draft 编译失败: {exc}") from exc
        engine, warnings = self._compose(spec)
        session = DraftSession(
            draft_id=draft_id,
            spec=spec,
            engine=engine,
            warnings=tuple(warnings),
        )
        self._sessions[draft_id] = session
        return session

    def session(self, draft_id: str) -> DraftSession:
        if draft_id not in self._sessions:
            raise DraftRuntimeError(f"unknown draft: {draft_id!r}")
        return self._sessions[draft_id]

    async def close(self) -> None:
        """销毁全部 Draft 会话（Studio 关闭调试面板时调用）。"""
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()

    # ------------------------------------------------------------ 内部

    def _compose(self, spec: HarnessSpec) -> tuple[Any, list[str]]:
        from ksadk.harness.skill_composition import compose_engine

        engine = compose_engine(
            spec,
            reasoner=self._reasoner,
            local_dir=self._local_dir,
            cache_dir=self._cache_dir,
            **self._engine_kwargs,
        )
        return engine, list(getattr(engine, "skill_warnings", ()))


__all__ = [
    "DRAFT_REF_PREFIX",
    "DRAFT_SESSION_PREFIX",
    "DraftRuntime",
    "DraftRuntimeError",
    "DraftSession",
]
