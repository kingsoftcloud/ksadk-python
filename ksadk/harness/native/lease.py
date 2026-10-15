"""Local worker ownership for a Native run, separate from loop decisions."""

from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar
from uuid import uuid4

from ksadk.harness.stores.native_contract import LeasedRunStore, RunStore

from .state import RunScope

Result = TypeVar("Result")


class RunLeaseController:
    def __init__(
        self, store: RunStore, scope: RunScope, *, ttl_seconds: float | None = None
    ) -> None:
        if ttl_seconds is not None:
            if not isinstance(store, LeasedRunStore):
                raise ValueError("leased Native runs require a durable lease store")
            if type(ttl_seconds) not in {int, float} or not 0 < ttl_seconds <= 3600:
                raise ValueError("lease TTL must be positive and at most one hour")
        self.store = store
        self.scope = scope
        self.ttl_seconds = ttl_seconds
        self.owner_id = uuid4().hex if ttl_seconds is not None else None
        self._fences: dict[str, int] = {}

    def attached(self, run_id: str) -> bool:
        return run_id in self._fences

    def acquire(self, run_id: str) -> None:
        if run_id in self._fences:
            return
        if self.ttl_seconds is None:
            self._fences[run_id] = self.store.acquire(self.scope, run_id)
        else:
            self._fences[run_id] = self.store.acquire_lease(
                self.scope, run_id, owner_id=self.owner_id, ttl_seconds=self.ttl_seconds
            )

    def token(self, run_id: str) -> int:
        if run_id not in self._fences:
            raise ValueError("attach the run before driving it")
        token = self._fences[run_id]
        if self.ttl_seconds is not None:
            self.store.renew_lease(
                self.scope, run_id, owner_id=self.owner_id,
                fencing_token=token, ttl_seconds=self.ttl_seconds,
            )
        return token

    def release(self, run_id: str) -> None:
        token = self._fences.get(run_id)
        if token is None:
            return
        if self.ttl_seconds is not None:
            self.store.release_lease(
                self.scope, run_id, owner_id=self.owner_id, fencing_token=token
            )
        self._fences.pop(run_id)

    async def keep_alive(self, run_id: str, effect: Awaitable[Result]) -> Result:
        """Renew during owned work or backoff; the Store fences late writes."""
        if self.ttl_seconds is None:
            return await effect
        stopped = asyncio.Event()

        async def heartbeat() -> None:
            interval = min(10.0, max(0.01, self.ttl_seconds / 3))
            while True:
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    self.token(run_id)

        task = asyncio.create_task(heartbeat())
        running = asyncio.ensure_future(effect)
        # Observe any result after bounded cancellation, including a port that
        # ignores cancellation. It cannot be returned to the old owner.
        running.add_done_callback(
            lambda finished: finished.exception() if not finished.cancelled() else None
        )
        try:
            completed, _ = await asyncio.wait(
                (running, task), return_when=asyncio.FIRST_COMPLETED
            )
            if task in completed:
                # A renewal error revokes local execution ownership now,
                # rather than after the external effect happens to return.
                task.result()
            return await running
        finally:
            stopped.set()
            if not running.done():
                running.cancel()
                # The dispatcher also bounds its own port cancellation. Never
                # make an uncooperative awaitable defeat lease-loss shutdown.
                await asyncio.wait((running,), timeout=0.5)
            await task
