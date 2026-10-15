"""G6 SDK 嵌入门面（总案 §6.2）：create_harness_session → HarnessSession。

pi 的 ``createAgentSession`` 一个函数即可嵌入任意 app；本模块把 KsADK 已有的
散装面（compile / start / stream / resume / steer / follow_up）组合成单一
门面。纯门面、不新造协议层：

- 底层是 :class:`~ksadk.harness.managed_runtime.ManagedHarnessRuntimeAdapter`
  （managed 引擎 = Steering + ContextSection + 治理全套能力）；
- ``HarnessSession.events`` 是事件订阅点（RuntimeEvent v2 流）；
- ``send`` / ``follow_up`` 是 G1 steering 语义（receipt 显式，不静默）；
- ``resume_approval`` 走既有 approval 合同；
- ``fork`` 委托 G2 一期 copy-on-read fork（未装配 SessionStore 时如实声明
  不支持——不伪造能力）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from ksadk.harness.engine.steering import SteerReceipt
from ksadk.harness.events import RuntimeEvent
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.session_store import SqliteSessionStore
from ksadk.harness.spec import HarnessSpec
from ksadk.runtime import CancelResult, RunHandle, StartRequest


@dataclass
class HarnessSessionOptions:
    """create_harness_session 的可选项（缺省全部取安全默认值）。"""

    reasoner: Any | None = None
    workspace_root: str | Path = "."
    #: 进程内 fork 支持；None = fork 能力如实声明为不支持。
    session_store: SqliteSessionStore | None = None
    tenant_id: str = "default"


class HarnessSession:
    """一次嵌入的会话门面：prompt → events → (send/follow_up/approve) → 终态。"""

    def __init__(
        self, spec: HarnessSpec, *, adapter: ManagedHarnessRuntimeAdapter | None = None,
        session_store: SqliteSessionStore | None = None, **adapter_kwargs: Any,
    ) -> None:
        self._spec = spec
        self._tenant_id = str(adapter_kwargs.pop("tenant_id", "default"))
        self._adapter = adapter or ManagedHarnessRuntimeAdapter(spec, **adapter_kwargs)
        self._session_store = session_store

    # ------------------------------------------------------------ 生命周期

    async def prompt(
        self,
        text: str,
        *,
        user_id: str = "local",
        session_id: str = "default",
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunHandle:
        """提交一轮用户输入，返回 RunHandle；用 :meth:`events` 订阅事件流。"""
        return await self._adapter.start(
            StartRequest(
                agent_id=agent_id or "default",
                user_id=user_id,
                session_id=session_id,
                input=text,
                runtime_type="harness",
                metadata=dict(metadata or {}),
            )
        )

    def events(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        """订阅该 Run 的事件流（follow_up 自动续跑共用同一条流）。"""
        return self._adapter.stream(handle)

    async def close(self, handle: RunHandle) -> None:
        await self._adapter.close(handle)

    async def close_all(self) -> None:
        await self._adapter.close_all()

    # ------------------------------------------------------------ 流中交互

    async def send(self, handle: RunHandle, text: str) -> SteerReceipt:
        """steer：任务不中断，消息在下一次模型调用并入输入（G1 §1.3）。"""
        return await self._adapter.steer(handle, text)

    async def follow_up(self, handle: RunHandle, text: str) -> SteerReceipt:
        """排队尾随指令，本次 Run 自然完成后自动续跑（G1 §1.3）。"""
        return await self._adapter.follow_up(handle, text)

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return await self._adapter.cancel(handle)

    async def resume_approval(
        self, handle: RunHandle, decision: str, *, call_id: str | None = None
    ) -> RunHandle:
        """审批恢复（复用既有 approval 合同；decision ∈ approved/denied）。"""
        from ksadk.runtime import ResumePayload, ResumeTarget

        target = ResumeTarget(
            kind="checkpoint_id", id=str(handle.native_ref.get("thread_id") or handle.run_id)
        )
        payload = ResumePayload(kind="approval_decision", data={"decision": decision}, call_id=call_id)
        return await self._adapter.resume(handle, target, payload)

    # ------------------------------------------------------------ 会话树

    def fork(
        self,
        *,
        source_session_id: str,
        from_entry_id: str | None = None,
        new_session_id: str,
    ) -> Any:
        """copy-on-read fork（G2 一期）。未装配 SessionStore 时明确报错。"""
        if self._session_store is None:
            raise RuntimeError(
                "fork 需要 HarnessSessionOptions.session_store；"
                "未装配时不伪造 fork 能力"
            )
        return self._session_store.fork(
            tenant_id=self._tenant_id,
            source_session_id=source_session_id,
            from_entry_id=from_entry_id,
            new_session_id=new_session_id,
        )

    @property
    def adapter(self) -> ManagedHarnessRuntimeAdapter:
        return self._adapter


def create_harness_session(
    spec: HarnessSpec, **options: Any
) -> HarnessSession:
    """五分钟嵌入入口（G6 §6.1）。

    示例::

        session = create_harness_session(spec)
        handle = await session.prompt("抓取今日 AI 新闻")
        async for event in session.events(handle):
            ...
    """
    return HarnessSession(spec, **options)


__all__ = ["HarnessSession", "HarnessSessionOptions", "create_harness_session"]
