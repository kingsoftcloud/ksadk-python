"""ADK / LangGraph 两个框架的 RuntimeAdapter (goal-07)。

在通用 :class:`~ksadk.runtime.runner_adapter.RunnerRuntimeAdapter` 之上,只声明
框架差异(resume 目标 + checkpoint 粒度),诚实暴露,不开接口后门:

- ADK:**forward-only**,resume 经 ``invocation_id``;不支持 time-travel/fork。
- LangGraph:**time-travel**,resume 经 ``checkpoint_id``;可按 turn 回滚/fork。

默认注册由 :mod:`ksadk.runtime.factory` 统一负责。
"""

from __future__ import annotations

from typing import Optional

from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import (
    CheckpointCapability,
    ResumePayload,
    ResumeTarget,
    RunHandle,
)
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


class ADKRuntimeAdapter(RunnerRuntimeAdapter):
    """ADK 框架 adapter(forward-only resume)。"""

    def __init__(self, runner: BaseRunner) -> None:
        super().__init__(runner, runtime_type="adk")

    async def _resume_native(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> Optional[dict]:
        # ADK forward-only:恢复目标 = invocation_id。注入 adk_runner 真消费的
        # ``checkpoint_resume`` + ``framework_ref.adk.invocation_id`` → run_async(invocation_id)。
        return {
            "checkpoint_resume": True,
            "run_id": target.id,
            "framework_ref": {"adk": {"invocation_id": target.id}},
            "input": payload.data if payload else None,
        }

    def _checkpoint_capability(self) -> CheckpointCapability:
        capability = super()._checkpoint_capability()
        if not capability.supported:
            return capability
        return capability.model_copy(
            update={
                "granularity": "delta",
                "rollback_scope": "invocation",
                "fork_supported": False,
            }
        )


class LangGraphRuntimeAdapter(RunnerRuntimeAdapter):
    """LangGraph 框架 adapter(time-travel resume)。"""

    def __init__(self, runner: BaseRunner) -> None:
        super().__init__(runner, runtime_type="langgraph")

    async def _resume_native(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> Optional[dict]:
        if target.kind != "checkpoint_id":
            raise ValueError(f"LangGraph resume requires checkpoint_id target, got {target.kind!r}")
        known_checkpoint_ids = {
            str(checkpoint_id)
            for checkpoint_id in handle.native_ref.get("known_checkpoint_ids", [])
        }
        if target.id not in known_checkpoint_ids:
            raise ValueError(f"checkpoint {target.id!r} does not belong to run {handle.run_id!r}")
        if payload is not None and payload.call_id:
            pending_approval_ids = {
                str(call_id) for call_id in handle.native_ref.get("pending_approval_ids", [])
            }
            if payload.call_id not in pending_approval_ids:
                raise ValueError(f"unknown interrupt {payload.call_id!r}")
        # LangGraph time-travel:恢复目标 = checkpoint_id。注入 langgraph_runner 真消费的
        # ``checkpoint_resume`` + ``framework_ref.langgraph.{checkpoint_id,thread_id}``。
        return {
            "checkpoint_resume": True,
            "run_id": handle.run_id,
            "resume_payload_provided": payload is not None,
            "resume_interrupt_id": payload.call_id if payload else None,
            "framework_ref": {
                "langgraph": {
                    "checkpoint_id": target.id,
                    "thread_id": str(handle.native_ref.get("thread_id") or handle.session_id),
                }
            },
            "input": payload.data if payload else None,
        }

    def _checkpoint_capability(self) -> CheckpointCapability:
        capability = super()._checkpoint_capability()
        if not capability.supported:
            return capability
        return capability.model_copy(
            update={
                "granularity": "snapshot",
                "rollback_scope": "turn",
                "fork_supported": True,
            }
        )


__all__ = [
    "ADKRuntimeAdapter",
    "LangGraphRuntimeAdapter",
]
