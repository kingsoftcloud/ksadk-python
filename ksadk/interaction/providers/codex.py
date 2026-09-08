# -*- coding: utf-8 -*-
"""Codex InteractionProvider：live JSON-RPC approval 回包（Phase 1 Task 6 Step 6）。

Codex 的 HITL 模型是**事件流 + 独立 live 命令通道**：审批卡阻塞在
``item/commandExecution/requestApproval``，回包必须经
:meth:`ksadk.codex.runtime.CodexRuntimeAdapter.submit` 以原 ``call_id`` 送达
**同一 client 实例**（thread 表在 adapter 进程内，换实例 = 回包丢失）。
本 provider 不重启流，也不伪造新 run。
"""

from __future__ import annotations

from typing import Any

from ksadk.interaction.contracts import (
    InteractionRecord,
    InteractionSubmission,
)
from ksadk.interaction.provider import (
    InteractionResolveContext,
    require_capability,
)
from ksadk.runtime.adapter import ResumePayload, RunHandle

# interaction action -> codex 原生 approval decision 词表。
_CODEX_APPROVAL_DECISIONS = {
    "approve": "approve",
    "reject": "deny",
    "cancel": "cancel",
}


class CodexInteractionProvider:
    """provider_id=codex，mode=live_submit（对齐 CodexRuntimeAdapter 矩阵）。"""

    provider_id = "codex"
    mode = "live_submit"

    async def resolve(
        self,
        context: InteractionResolveContext,
        record: InteractionRecord,
        submission: InteractionSubmission,
    ) -> RunHandle:
        require_capability(
            context, "submit_interaction", provider_id=self.provider_id
        )
        native_target = record.native_target or {}
        call_id = str(native_target.get("call_id") or record.interaction_id)
        if not call_id:
            raise ValueError("codex interaction requires a native call_id")
        if record.kind == "approval":
            payload_kind = "approval_decision"
            data = self._approval_data(submission.response, submission.action)
        else:
            payload_kind = "hitl_answer"
            data = self._structured_data(submission.response)
        await context.adapter.submit(
            context.handle,
            ResumePayload(kind=payload_kind, call_id=call_id, data=data),
        )
        # live_submit 不换 handle、不重启流：回包送达后原 stream 自然续跑。
        return context.handle

    @staticmethod
    def _approval_data(response: Any, action: str) -> Any:
        """approve/reject 映射为 codex 原生 decision；显式 response 优先。"""

        decision = _CODEX_APPROVAL_DECISIONS.get(str(action), str(action))
        if isinstance(response, dict):
            data = {
                key: value for key, value in response.items() if value is not None
            }
            if not any(key in data for key in ("decision", "name")):
                data["decision"] = decision
            elif "decision" in data:
                # The runtime advertises the public Interaction vocabulary
                # (decision enum approve/reject) in request_schema; a client
                # echoing it must be normalized to the codex-native word
                # instead of failing the client vocab check fail-closed.
                data["decision"] = _CODEX_APPROVAL_DECISIONS.get(
                    str(data["decision"]), str(data["decision"])
                )
            return data
        return {"decision": decision}

    @staticmethod
    def _structured_data(response: Any) -> Any:
        if isinstance(response, dict):
            return dict(response)
        return {"answer": response}


__all__ = ["CodexInteractionProvider"]
