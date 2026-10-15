"""Admission fencing for Studio plugin-runtime maintenance windows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ksadk.plugins.host import PluginHostError

_T = TypeVar("_T")


class RuntimeAdmissionGate:
    """Wait through bounded profile refreshes without admitting stale hosts."""

    def __init__(self) -> None:
        self._open = True
        self._ready = asyncio.Event()
        self._ready.set()

    @property
    def is_open(self) -> bool:
        return self._open

    def suspend(self) -> None:
        self._open = False
        self._ready.clear()

    def resume(self) -> None:
        self._open = True
        self._ready.set()

    def close(self) -> None:
        self._open = False
        self._ready.set()

    async def wait(self) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=60)
        except TimeoutError as exc:
            raise PluginHostError(
                "plugin_runtime_reconfiguring",
                "Studio plugin runtime is still reconfiguring after 60 seconds",
            ) from exc

    async def resolve(self, factory: Callable[[], Awaitable[_T]]) -> _T:
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            try:
                return await factory()
            except PluginHostError as exc:
                if exc.code != "plugin_runtime_reconfiguring" or (
                    asyncio.get_running_loop().time() >= deadline
                ):
                    raise
                await asyncio.sleep(0)
