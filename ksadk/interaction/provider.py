# -*- coding: utf-8 -*-
"""InteractionProvider seam（Phase 1 Task 6 Step 1）。

Interaction 回包的分发 seam：Worker 载入权威 :class:`InteractionRecord`
后，把回包交给 record 绑定的 provider，由 provider 用 **activation 持有的**
``RuntimeAdapter``/``RunHandle`` 以框架原生方式送达：

- ``live_submit``：runtime 有 live 命令通道（如 Codex JSON-RPC approval），
  回包经 ``adapter.submit`` 原路送达同一 client 实例，不重启流。
- ``durable_resume``：runtime 以 checkpoint/continuation 收口（如
  LangGraph），回包映射为存的 checkpoint/thread target 经 ``adapter.resume``
  恢复执行。
- ``unavailable``：生产 Adapter 无法以框架原生身份送达回包时**诚实拒绝**，
  绝不静默重放一个新 run 冒充 resume。

provider 的 mode 必须与 adapter 的
:class:`~ksadk.kernel.contracts.RuntimeCapabilityMatrix` 一致：mode 只是
静态声明，``resolve`` 内部仍逐次校验当前 adapter 的真实 capability，
不一致时 fail closed（``runtime_interaction_unavailable``，不标 resolved）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ksadk.interaction.contracts import (
    InteractionRecord,
    InteractionSubmission,
)
from ksadk.runtime.adapter import RunHandle, RuntimeAdapter

InteractionProviderMode = Literal["live_submit", "durable_resume", "unavailable"]

RUNTIME_INTERACTION_UNAVAILABLE = "runtime_interaction_unavailable"
"""provider 无法以框架原生身份送达回包时的稳定错误码（typed rejection）。"""


@dataclass(frozen=True)
class InteractionResolveContext:
    """一次回包分发的执行上下文——全部来自当前 activation 的 ActiveExecution。"""

    adapter: RuntimeAdapter
    handle: RunHandle
    activation_id: str
    fencing_token: int


@runtime_checkable
class InteractionProvider(Protocol):
    """把 Interaction 回包映射为框架原生 resume/submit 的 provider 协议。"""

    provider_id: str
    mode: InteractionProviderMode

    async def resolve(
        self,
        context: InteractionResolveContext,
        record: InteractionRecord,
        submission: InteractionSubmission,
    ) -> RunHandle: ...


def require_capability(
    context: InteractionResolveContext,
    capability_name: str,
    *,
    provider_id: str,
) -> None:
    """fail-closed capability 校验：mode 声明与真实 adapter 能力不一致即拒绝。"""

    from ksadk.kernel.errors import AgentKernelError

    capability = getattr(context.adapter.capabilities(), capability_name, None)
    if capability is None or not capability.supported:
        reason = getattr(capability, "reason", "not_implemented") or "not_implemented"
        raise AgentKernelError(
            RUNTIME_INTERACTION_UNAVAILABLE,
            f"interaction provider {provider_id!r} requires adapter capability "
            f"{capability_name!r}, which is unavailable: {reason}",
            retryable=False,
            details={
                "provider_id": provider_id,
                "capability": capability_name,
                "reason": reason,
            },
        )


class UnavailableInteractionProvider:
    """诚实占位：该 runtime 的回包送达路径尚未实现。"""

    provider_id = ""
    mode: InteractionProviderMode = "unavailable"

    async def resolve(
        self,
        context: InteractionResolveContext,
        record: InteractionRecord,
        submission: InteractionSubmission,
    ) -> RunHandle:
        from ksadk.kernel.errors import AgentKernelError

        provider_id = type(self).provider_id or record.provider_id
        raise AgentKernelError(
            RUNTIME_INTERACTION_UNAVAILABLE,
            f"interaction provider for {provider_id!r} is unavailable: "
            "runtime cannot deliver an interaction response with its native "
            "identity; refusing to replay a new run",
            retryable=False,
            details={"provider_id": provider_id, "mode": "unavailable"},
        )


__all__ = [
    "RUNTIME_INTERACTION_UNAVAILABLE",
    "InteractionProvider",
    "InteractionProviderMode",
    "InteractionResolveContext",
    "UnavailableInteractionProvider",
    "require_capability",
]
