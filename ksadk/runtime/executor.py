"""RuntimeAdapter 的统一生命周期路由与 Handle 所有权。

``_runs`` 只是当前进程的 handle cache：status、幂等、恢复资格和 owner 判断的
真相在 ``AgentKernelStore`` 的 durable Run 行；cache miss 不能等价于 Run 不
存在（见 :meth:`RuntimeExecutor.resolve_run`）。
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ksadk.runtime.adapter import (
    CancelResult,
    CheckpointDescriptor,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.launch import RuntimeLaunchContext

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ksadk.kernel.store import AgentKernelStore, RunRecord

_HandleKey = tuple[str, str, str]


class RunNotFoundError(LookupError):
    """durable Store 中不存在该 Run；cache miss 不是证据，必须查 Store。"""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"durable run not found: {run_id!r}")
        self.run_id = run_id


@dataclass
class DurableRun:
    """Store 中的 Run 真相 + 本进程 live handle（可能未 attach）。"""

    run: "RunRecord"
    live_handle: RunHandle | None = None


@dataclass
class _OwnedRun:
    adapter: RuntimeAdapter
    handle: RunHandle


@dataclass
class RuntimeStartPreparation:
    """One adapter instance preflighted for exactly one subsequent start."""

    context: RuntimeLaunchContext
    adapter: RuntimeAdapter
    consumed: bool = False


class RuntimeExecutor:
    """让每个 Handle 始终回到创建或恢复它的 Adapter 实例。"""

    def __init__(
        self,
        registry: RuntimeRegistry,
        *,
        kernel_store: "AgentKernelStore | None" = None,
    ) -> None:
        self._registry = registry
        self._kernel_store = kernel_store
        self._runs: dict[_HandleKey, _OwnedRun] = {}

    async def resolve_run(self, run_id: str) -> DurableRun:
        """以 durable Store 为真相解析 Run；cache 只是 live handle 提示。"""

        durable = None
        if self._kernel_store is not None:
            durable = await self._kernel_store.load_run(run_id)
        if durable is None:
            # 没有 Store 时只能退回 cache；cache miss 不等价于 Run 不存在，
            # 因此未配置 kernel_store 的旧调用方仍需显式处理缺失。
            if self._kernel_store is not None:
                raise RunNotFoundError(run_id)
            cached = next(
                (
                    owned.handle
                    for (_, rid, _), owned in self._runs.items()
                    if rid == run_id
                ),
                None,
            )
            if cached is None:
                raise RunNotFoundError(run_id)
            return DurableRun(run=_cache_only_record(cached), live_handle=cached)
        live = next(
            (
                owned.handle
                for (_, rid, _), owned in self._runs.items()
                if rid == run_id
            ),
            None,
        )
        return DurableRun(run=durable, live_handle=live)

    async def prepare_start(self, context: RuntimeLaunchContext) -> RuntimeStartPreparation:
        """Preflight a fresh adapter and retain it for the matching ``start``.

        A response streaming endpoint must learn about lazy import/configuration
        failures before its SSE headers are committed.  Retaining the instance
        matters: creating a second adapter after preflight could execute user
        agent initialization twice.
        """

        adapter = self._registry.create(context)
        await adapter.preflight()
        return RuntimeStartPreparation(context=context, adapter=adapter)

    async def start(
        self,
        context: RuntimeLaunchContext,
        request: StartRequest,
        *,
        preparation: RuntimeStartPreparation | None = None,
    ) -> RunHandle:
        adapter = self._take_prepared_adapter(context, preparation)
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
            raise RuntimeError(f"runtime handle is already attached: {_handle_key(handle)!r}")
        self._record_owner(adapter, handle)
        return handle

    def stream(self, handle: RunHandle):
        owned = self._resolve(handle)
        return owned.adapter.stream(owned.handle)

    async def cancel(self, handle: RunHandle) -> CancelResult:
        owned = self._resolve(handle)
        return await owned.adapter.cancel(owned.handle)

    async def pause(self, handle: RunHandle) -> PauseResult:
        owned = self._resolve(handle)
        return await owned.adapter.pause(owned.handle)

    async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
        owned = self._resolve(handle)
        await owned.adapter.submit(owned.handle, payload)

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

    async def close_all(self) -> None:
        """Release every attached adapter while preserving best-effort cleanup."""

        first_error: BaseException | None = None
        for key, owned in list(self._runs.items()):
            try:
                await owned.adapter.close(owned.handle)
            except BaseException as exc:  # cleanup must continue for the other runtimes
                if first_error is None:
                    first_error = exc
            finally:
                self._runs.pop(key, None)
        if first_error is not None:
            raise first_error

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

    def find_handle(
        self,
        runtime_type: str,
        run_id: str,
        session_id: str,
    ) -> RunHandle | None:
        """Return the attached handle for one exact runtime/run/session scope."""

        owned = self._runs.get(
            (
                _normalize_runtime_type(runtime_type),
                str(run_id),
                str(session_id),
            )
        )
        return owned.handle if owned is not None else None

    def native_capabilities(self, context: RuntimeLaunchContext) -> dict[str, object]:
        """Read the capability declaration from the registered Runtime implementation."""

        adapter = self._registry.create(context)
        return dict(adapter.runtime.native_capabilities())

    def registered_runtime_types(self) -> list[str]:
        """Expose Registry membership without leaking or duplicating the Registry."""

        return self._registry.registered_types()

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

    def _take_prepared_adapter(
        self,
        context: RuntimeLaunchContext,
        preparation: RuntimeStartPreparation | None,
    ) -> RuntimeAdapter:
        if preparation is None:
            return self._registry.create(context)
        if preparation.consumed:
            raise RuntimeError("runtime start preparation was already consumed")
        if preparation.context != context:
            raise ValueError("runtime start preparation does not match launch context")
        preparation.consumed = True
        return preparation.adapter


def _cache_only_record(handle: RunHandle) -> "RunRecord":
    from ksadk.kernel.state import RunState
    from ksadk.kernel.store import RunRecord

    # 无 kernel_store 的旧调用方路径：state 只能标记 pending，真相以 Store 为准。
    return RunRecord(
        run_id=handle.run_id,
        agent_instance_id="",
        session_id=handle.session_id,
        state=RunState.PENDING,
        metadata={"source": "process_cache", "runtime_type": handle.runtime_type},
    )


def _normalize_runtime_type(runtime_type: str) -> str:
    return runtime_type.strip().lower()


def _handle_key(handle: RunHandle) -> _HandleKey:
    return (
        _normalize_runtime_type(handle.runtime_type),
        handle.run_id,
        handle.session_id,
    )


__all__ = [
    "RuntimeExecutor",
    "RuntimeStartPreparation",
    "DurableRun",
    "RunNotFoundError",
]
