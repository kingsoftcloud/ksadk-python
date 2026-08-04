"""RuntimeAdapter 的统一生命周期路由与 Handle 所有权。"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from ksadk.runtime.adapter import (
    CancelResult,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.launch import RuntimeLaunchContext

_HandleKey = tuple[str, str, str]


@dataclass
class _OwnedRun:
    adapter: RuntimeAdapter
    handle: RunHandle


class RuntimeExecutor:
    """让每个 Handle 始终回到创建或恢复它的 Adapter 实例。"""

    def __init__(self, registry: RuntimeRegistry) -> None:
        self._registry = registry
        self._runs: dict[_HandleKey, _OwnedRun] = {}

    async def start(
        self,
        context: RuntimeLaunchContext,
        request: StartRequest,
    ) -> RunHandle:
        adapter = self._registry.create(context)
        handle = await adapter.start(request)
        expected_type = _normalize_runtime_type(context.runtime_type)
        if _normalize_runtime_type(handle.runtime_type) != expected_type:
            with suppress(Exception):
                await adapter.close(handle)
            raise ValueError(
                "adapter returned a handle with the wrong runtime type: "
                f"expected {expected_type!r}, got {handle.runtime_type!r}"
            )
        if handle.session_id != request.session_id:
            with suppress(Exception):
                await adapter.close(handle)
            raise ValueError(
                "adapter returned a handle for the wrong session: "
                f"expected {request.session_id!r}, got {handle.session_id!r}"
            )
        if _handle_key(handle) in self._runs:
            with suppress(Exception):
                await adapter.close(handle)
            raise RuntimeError(
                f"runtime handle is already attached: {_handle_key(handle)!r}"
            )
        self._record_owner(adapter, handle)
        return handle

    def stream(self, handle: RunHandle):
        owned = self._resolve(handle)
        return owned.adapter.stream(owned.handle)

    async def cancel(self, handle: RunHandle) -> CancelResult:
        owned = self._resolve(handle)
        return await owned.adapter.cancel(owned.handle)

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        old_key = _handle_key(handle)
        owned = self._resolve(handle)
        resumed = await owned.adapter.resume(owned.handle, target, payload)
        if _normalize_runtime_type(resumed.runtime_type) != old_key[0]:
            raise ValueError("resumed handle changed runtime type")
        if resumed.session_id != old_key[2]:
            raise ValueError("resumed handle changed session")

        new_key = _handle_key(resumed)
        if new_key != old_key:
            existing = self._runs.get(new_key)
            if existing is not None and existing.adapter is not owned.adapter:
                raise RuntimeError(f"runtime handle is already attached: {new_key!r}")
            self._runs.pop(old_key, None)
        self._runs[new_key] = _OwnedRun(adapter=owned.adapter, handle=resumed)
        return resumed

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        owned = self._resolve(handle)
        return await owned.adapter.checkpoint(owned.handle)

    async def close(self, handle: RunHandle) -> None:
        key = _handle_key(handle)
        owned = self._resolve(handle)
        try:
            await owned.adapter.close(owned.handle)
        finally:
            self._runs.pop(key, None)

    async def attach(
        self,
        context: RuntimeLaunchContext,
        handle: RunHandle,
    ) -> RunHandle:
        expected_type = _normalize_runtime_type(context.runtime_type)
        if _normalize_runtime_type(handle.runtime_type) != expected_type:
            raise ValueError("launch context and persisted handle runtime type differ")
        if self.is_attached(handle):
            return handle

        adapter = self._registry.create(context)
        restored = await adapter.attach(handle)
        if restored != handle:
            with suppress(Exception):
                await adapter.close(restored)
            raise ValueError("adapter attach must preserve persisted handle identity")
        self._record_owner(adapter, restored)
        return restored

    def is_attached(self, handle: RunHandle) -> bool:
        owned = self._runs.get(_handle_key(handle))
        return owned is not None and owned.handle == handle

    def _resolve(self, handle: RunHandle) -> _OwnedRun:
        key = _handle_key(handle)
        try:
            owned = self._runs[key]
        except KeyError:
            raise KeyError(f"runtime handle is not attached: {key!r}") from None
        if owned.handle != handle:
            raise ValueError(f"runtime handle payload does not match owner: {key!r}")
        return owned

    def _record_owner(self, adapter: RuntimeAdapter, handle: RunHandle) -> None:
        key = _handle_key(handle)
        if key in self._runs:
            raise RuntimeError(f"runtime handle is already attached: {key!r}")
        self._runs[key] = _OwnedRun(adapter=adapter, handle=handle)


def _normalize_runtime_type(runtime_type: str) -> str:
    return runtime_type.strip().lower()


def _handle_key(handle: RunHandle) -> _HandleKey:
    return (
        _normalize_runtime_type(handle.runtime_type),
        handle.run_id,
        handle.session_id,
    )


__all__ = ["RuntimeExecutor"]
