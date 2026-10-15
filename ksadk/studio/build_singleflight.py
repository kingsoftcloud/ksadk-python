"""Join in-flight builds; cancellation of one UI request does not own the build."""

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, TypeVar

T = TypeVar("T")


class BuildSingleFlight:
    def __init__(self):
        self.pending: dict[tuple[str, Hashable | None], asyncio.Task[Any]] = {}

    async def run(
        self, agent_id: str, build: Callable[[], Awaitable[T]], *,
        identity: Callable[[], Hashable] | None = None,
    ) -> T:
        while True:
            revision = identity() if identity is not None else None
            key = (agent_id, revision)
            task = self.pending.get(key)
            if task is None:
                task = asyncio.create_task(build())
                self.pending[key] = task

                def settled(completed, owned_key=key):
                    if self.pending.get(owned_key) is completed:
                        self.pending.pop(owned_key, None)
                    if not completed.cancelled():
                        completed.exception()  # Observe failures after all UI waiters detach.

                task.add_done_callback(settled)
            result = await asyncio.shield(task)
            # Saving during prewarm does not authorize a later turn to execute
            # that older revision. Other waiters still own the old task.
            if identity is None or identity() == revision:
                return result
