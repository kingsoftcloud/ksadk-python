"""G1 流式中 Steering 的队列载体与引擎侧投递操作
（设计文档 docs/harness-pi-gap-alignment-plan.md §1.3.2）。

每个 Run 一组 FIFO 队列；本模块只存取，不做活跃性判断——run 是否活跃由
ManagedLangGraphEngine 决定（未知/终态 Run 的 steer 在引擎层升级或拒绝）。
单事件循环内使用，无需跨线程锁。

引擎把 steer/follow_up 的实现放在这里（而非 langgraph.py），是为了守住
langgraph.py <1000 行的模块体积合同；运行控制侧的同类 seam 见
run_control_bridge.append_controlled_stop。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SteerReceipt:
    """steer/follow_up 投递回执——失败可见，不静默成功。"""

    status: str  # "accepted" | "upgraded_to_follow_up" | "rejected"
    mode: str  # "steer" | "follow_up"
    reason: str = ""


class SteeringQueue:
    def __init__(self) -> None:
        self._steering: dict[str, list[str]] = {}
        self._follow_ups: dict[str, list[str]] = {}

    def push_steering(self, run_id: str, text: str) -> None:
        self._steering.setdefault(run_id, []).append(text)

    def push_follow_up(self, run_id: str, text: str) -> None:
        self._follow_ups.setdefault(run_id, []).append(text)

    def drain_steering(self, run_id: str) -> list[str]:
        return self._steering.pop(run_id, [])

    def pop_follow_up(self, run_id: str) -> str | None:
        messages = self._follow_ups.get(run_id)
        if not messages:
            return None
        return messages.pop(0)

    def discard(self, run_id: str) -> None:
        """丢弃该 Run 的全部残留条目（终态清理，幂等）。

        Run 进入终态（cancel / RunControlStop）或 close 后调用：未消费的
        steering 与未 pop 的 follow_up 都不应继续驻留，否则已结束的 Run 仍会
        被续跑，或进程内长期占内存。注意 Run 自然完成（RUN_COMPLETED）不能
        调用它——adapter 依赖 follow_up 续跑下一个 Run。
        """
        self._steering.pop(run_id, None)
        self._follow_ups.pop(run_id, None)


def _reject_terminal(run: Any, mode: str) -> SteerReceipt | None:
    """终态无消费者判定：CANCELED/FAILED 后的 steer/follow_up 一律拒绝。

    与 unknown_run 一致——rejected 不发 STEER_RECEIVED。COMPLETED 由调用方
    各自处理（steer 升级 follow_up、follow_up 入队），不属于本判定。
    """
    from ksadk.harness.state import RunStatus

    if run.state.status in (RunStatus.CANCELED, RunStatus.FAILED):
        return SteerReceipt(
            status="rejected", mode=mode, reason="run_terminal_no_consumer"
        )
    return None


def steer_run(
    engine: Any, handle: Any, text: str, *, allow_follow_up: bool = True
) -> SteerReceipt:
    """流式中投递纠偏指令：下一个 reason 节点边界前生效。

    Run 未知 → rejected；CANCELED/FAILED 终态无消费者 → rejected；其余终态
    → 升级为 follow_up 语义（receipt 与 STEER_RECEIVED 事件均如实标注）。
    """
    from ksadk.harness.events import EventType

    run = engine._runs.get(handle.run_id)
    if run is None:
        return SteerReceipt(status="rejected", mode="steer", reason="unknown_run")
    rejected = _reject_terminal(run, mode="steer")
    if rejected is not None:
        return rejected
    if run.done:
        if not allow_follow_up:
            return SteerReceipt(
                status="rejected", mode="steer", reason="run_already_terminal"
            )
        engine._steering.push_follow_up(handle.run_id, text)
        run.events.append(
            engine._event(
                run,
                EventType.STEER_RECEIVED,
                {"source": "user", "mode": "steer", "accepted": False, "upgraded": True},
            )
        )
        return SteerReceipt(
            status="upgraded_to_follow_up", mode="steer",
            reason="run_already_terminal",
        )
    engine._steering.push_steering(handle.run_id, text)
    run.events.append(
        engine._event(
            run,
            EventType.STEER_RECEIVED,
            {"source": "user", "mode": "steer", "accepted": True, "upgraded": False},
        )
    )
    return SteerReceipt(status="accepted", mode="steer")


def follow_up_run(engine: Any, handle: Any, text: str) -> SteerReceipt:
    """排队尾随指令：本次 Run 自然完成后自动作为下一轮 prompt 提交。"""
    from ksadk.harness.events import EventType

    run = engine._runs.get(handle.run_id)
    if run is None:
        return SteerReceipt(status="rejected", mode="follow_up", reason="unknown_run")
    rejected = _reject_terminal(run, mode="follow_up")
    if rejected is not None:
        return rejected
    engine._steering.push_follow_up(handle.run_id, text)
    run.events.append(
        engine._event(
            run,
            EventType.STEER_RECEIVED,
            {"source": "user", "mode": "follow_up", "accepted": True, "upgraded": False},
        )
    )
    return SteerReceipt(status="accepted", mode="follow_up")


def upgrade_leftover_steering(engine: Any, run: Any) -> None:
    """自然完成收尾：把残留 steering 条目升级进 follow_up 队列。

    最后一次 reason drain 之后、run.done=True 之前到达的 steer 拿到了
    accepted 回执，却再也不会被任何 reason 边界消费；不升级就会滞留到
    close()。逐条补发 STEER_RECEIVED（mode="steer", accepted=False,
    upgraded=True），与「已终态 steer 升级」的事件形态保持一致，使
    「RECEIVED(accepted) 无 DELIVERED」的不一致在事件流中可观测。
    """
    from ksadk.harness.events import EventType

    leftovers = engine._steering.drain_steering(run.handle.run_id)
    for text in leftovers:
        engine._steering.push_follow_up(run.handle.run_id, text)
        run.events.append(
            engine._event(
                run,
                EventType.STEER_RECEIVED,
                {"source": "user", "mode": "steer", "accepted": False, "upgraded": True},
            )
        )


def follow_up_origin(run: Any) -> dict[str, str]:
    """RUN_STARTED payload 的续跑标记：只对 follow_up 起的 Run 标注 origin。"""
    if run.request.metadata.get("origin") == "follow_up":
        return {"origin": "follow_up"}
    return {}


__all__ = [
    "SteerReceipt",
    "SteeringQueue",
    "follow_up_origin",
    "follow_up_run",
    "steer_run",
    "upgrade_leftover_steering",
]
