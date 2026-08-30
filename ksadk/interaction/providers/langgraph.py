# -*- coding: utf-8 -*-
"""LangGraph InteractionProvider：checkpoint resume 回包（Phase 1 Task 6 Step 6）。

LangGraph 的 HITL 模型是 interrupt + checkpoint：graph 停在
``__interrupt__``，durable 状态保存在 thread 的 checkpoint。回包必须映射为
request 时存的 checkpoint/thread target，经
:meth:`ksadk.runtime.framework_adapters.LangGraphRuntimeAdapter.resume`
恢复**同一个 thread**（time-travel 语义），绝不新起一个 run。
"""

from __future__ import annotations

from ksadk.interaction.contracts import (
    InteractionRecord,
    InteractionSubmission,
)
from ksadk.interaction.provider import (
    InteractionResolveContext,
    require_capability,
)
from ksadk.runtime.adapter import ResumePayload, ResumeTarget, RunHandle


class LangGraphInteractionProvider:
    """provider_id=langgraph，mode=durable_resume（对齐 checkpoint 矩阵）。"""

    provider_id = "langgraph"
    mode = "durable_resume"

    async def resolve(
        self,
        context: InteractionResolveContext,
        record: InteractionRecord,
        submission: InteractionSubmission,
    ) -> RunHandle:
        require_capability(context, "resume", provider_id=self.provider_id)
        native_target = record.native_target or {}
        checkpoint_id = str(native_target.get("checkpoint_id") or "")
        if not checkpoint_id:
            raise ValueError(
                "langgraph interaction requires a stored checkpoint_id target"
            )
        thread_id = str(native_target.get("thread_id") or context.handle.session_id)
        context.handle.native_ref.setdefault("thread_id", thread_id)
        payload_kind = "approval_decision" if record.kind == "approval" else "hitl_answer"
        payload = ResumePayload(
            kind=payload_kind,
            call_id=str(native_target.get("call_id") or record.interaction_id),
            data=submission.response,
        )
        return await context.adapter.resume(
            context.handle,
            ResumeTarget(kind="checkpoint_id", id=checkpoint_id),
            payload,
        )


__all__ = ["LangGraphInteractionProvider"]
