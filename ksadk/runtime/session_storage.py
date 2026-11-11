"""Bind one app's Session target to every native adapter it creates."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from ksadk.sessions.topology import StorageTarget

logger = logging.getLogger(__name__)


class RunnerSessionStorageMixin:
    def bind_effective_session_target(self, target: StorageTarget) -> None:
        self._runner.bind_effective_session_target(target)


def bind_adapter_session_target(adapter: Any, target: StorageTarget | None) -> None:
    bind = getattr(adapter, "bind_effective_session_target", None)
    if target is not None and callable(bind):
        bind(target)


class ExecutorSessionStorageMixin:
    def bind_effective_session_target(self, context: Any, target: StorageTarget) -> None:
        bindings = getattr(self, "_session_storage_bindings", {})
        previous = bindings.get(id(context))
        if previous is not None and previous != (context, target):
            raise RuntimeError("RUNTIME_SESSION_CONFLICT: app storage was already selected")
        if previous is None and self._runs:
            raise RuntimeError("RUNTIME_SESSION_CONFLICT: cannot select storage during execution")
        cached = self._capability_adapters.get(id(context))
        if cached is not None and cached[0] is context:
            bind_adapter_session_target(cached[1], target)
        bindings[id(context)] = (context, target)
        self._session_storage_bindings = bindings

    def _session_storage_target(self, context: Any) -> StorageTarget | None:
        bound = getattr(self, "_session_storage_bindings", {}).get(id(context))
        return bound[1] if bound is not None and bound[0] is context else None


async def close_preflight_adapter(adapter: Any) -> None:
    close = getattr(adapter, "close_app_resources", None)
    if callable(close):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("Native preflight resource cleanup failed: %s", type(exc).__name__)


def bind_session_adapter_provider(
    provider: Callable[[], Any] | None,
    target: StorageTarget,
    created: list[Any] | None = None,
) -> Callable[[], Any] | None:
    if provider is None:
        return None

    def create_adapter() -> Any:
        adapter = provider()
        if created is not None and not created:
            created.append(adapter)
        bind_adapter_session_target(adapter, target)
        return adapter

    return create_adapter
