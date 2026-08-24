# -*- coding: utf-8 -*-
"""框架原生 InteractionProvider 注册表（Phase 1 Task 6）。

provider 是无状态映射器（adapter/handle 由
:class:`~ksadk.interaction.provider.InteractionResolveContext` 注入），
因此注册表返回共享实例即可。key 同时覆盖 ``provider_id`` 与
``runtime_type``（当前两者一致：codex/langgraph/adk）。
"""

from __future__ import annotations

from typing import Mapping

from ksadk.interaction.provider import (
    InteractionProvider,
    UnavailableInteractionProvider,
)
from ksadk.interaction.providers.adk import ADKInteractionProvider
from ksadk.interaction.providers.codex import CodexInteractionProvider
from ksadk.interaction.providers.langgraph import LangGraphInteractionProvider


def default_interaction_providers() -> dict[str, InteractionProvider]:
    """默认 provider 注册表：provider_id / runtime_type -> provider。"""

    providers: list[InteractionProvider] = [
        CodexInteractionProvider(),
        LangGraphInteractionProvider(),
        ADKInteractionProvider(),
    ]
    registry: dict[str, InteractionProvider] = {}
    for provider in providers:
        registry[provider.provider_id] = provider
    return registry


def provider_for(
    registry: Mapping[str, InteractionProvider], runtime_type: str
) -> InteractionProvider:
    """按 runtime_type 取 provider；未知 runtime 诚实返回 unavailable 占位。"""

    return registry.get(runtime_type, UnavailableInteractionProvider())


__all__ = [
    "ADKInteractionProvider",
    "CodexInteractionProvider",
    "LangGraphInteractionProvider",
    "UnavailableInteractionProvider",
    "default_interaction_providers",
    "provider_for",
]
