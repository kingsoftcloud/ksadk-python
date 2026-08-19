# -*- coding: utf-8 -*-
"""ADK InteractionProvider：诚实声明 unavailable（Phase 1 Task 6 Step 6）。

ADK 的 confirmation/function-response 回包语义上要求以原 invocation 的
native 身份续跑；当前生产 ``ADKRuntimeAdapter``（forward-only resume 经
invocation_id）无法在一次 Interaction 回包中保留该 native 身份——
``submit_interaction`` capability 是 unavailable（无 live 命令通道），
resume 则会以新 invocation 重放。因此本 provider 诚实 advertise
``unavailable`` 并 fail closed，**绝不静默重放一个新 run 冒充回包送达**。
"""

from __future__ import annotations

from ksadk.interaction.contracts import (
    InteractionRecord,
    InteractionSubmission,
)
from ksadk.interaction.provider import (
    InteractionResolveContext,
    UnavailableInteractionProvider,
)
from ksadk.runtime.adapter import RunHandle


class ADKInteractionProvider(UnavailableInteractionProvider):
    """provider_id=adk，mode=unavailable（对齐 RunnerRuntimeAdapter 矩阵）。"""

    provider_id = "adk"
    mode = "unavailable"

    async def resolve(
        self,
        context: InteractionResolveContext,
        record: InteractionRecord,
        submission: InteractionSubmission,
    ) -> RunHandle:
        return await super().resolve(context, record, submission)


__all__ = ["ADKInteractionProvider"]
