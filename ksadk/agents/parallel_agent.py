"""并行编排 agent。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

from ksadk.agents.base import BaseOrchestrationAgent
from ksadk.agents.context import OrchestrationContext
from ksadk.agents.event import AgentEvent, EventType

_MISSING = object()


class ParallelAgent(BaseOrchestrationAgent):
    """在独立分支上下文中并发执行子 agent。"""

    async def _run_impl(self, context: OrchestrationContext) -> AsyncGenerator[AgentEvent, None]:
        if not self.sub_agents:
            return

        queue: asyncio.Queue[tuple[str, AgentEvent | None]] = asyncio.Queue()
        branch_contexts: dict[str, OrchestrationContext] = {}
        base_state = context.snapshot_state()
        branch_errors: list[Exception] = []
        tasks: list[asyncio.Task[None]] = []

        async def run_branch(
            agent_name: str,
            sub_agent: Any,
            branch_context: OrchestrationContext,
        ) -> None:
            try:
                async for event in self._run_sub_agent(sub_agent, branch_context):
                    if not event.branch:
                        event = replace(event, branch=branch_context.branch)
                    await queue.put((agent_name, event))
            except Exception as exc:
                branch_errors.append(exc)
            finally:
                await queue.put((agent_name, None))

        for sub_agent in self.sub_agents:
            agent_name = self._get_agent_name(sub_agent)
            branch_context = context.create_branch(agent_name)
            branch_contexts[agent_name] = branch_context
            tasks.append(asyncio.create_task(run_branch(agent_name, sub_agent, branch_context)))

        finished = 0
        try:
            while finished < len(tasks):
                _, item = await queue.get()
                if item is None:
                    finished += 1
                    continue
                yield item
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

        if branch_errors:
            raise branch_errors[0]

        merge_delta = self._merge_branch_states(context, base_state, branch_contexts)
        if merge_delta:
            yield AgentEvent(
                agent_name=self.name,
                event_type=EventType.STATE_CHANGE,
                branch=context.branch,
                data=merge_delta,
                state_delta=merge_delta,
            )

    def _merge_branch_states(
        self,
        context: OrchestrationContext,
        base_state: dict[str, Any],
        branch_contexts: dict[str, OrchestrationContext],
    ) -> dict[str, Any]:
        branch_deltas: dict[str, dict[str, Any]] = {}
        by_key: dict[str, dict[str, Any]] = {}

        for branch_name, branch_context in branch_contexts.items():
            delta: dict[str, Any] = {}
            for key, value in branch_context.state.items():
                if base_state.get(key, _MISSING) != value:
                    delta[key] = value
                    by_key.setdefault(key, {})[branch_name] = value
            branch_deltas[branch_name] = delta

        merge_delta: dict[str, Any] = {f"{self.name}_results": branch_deltas}
        context.set(f"{self.name}_results", branch_deltas)

        conflicts: dict[str, dict[str, Any]] = {}
        for key, branch_values in by_key.items():
            values = list(branch_values.values())
            first = values[0]
            if all(value == first for value in values[1:]):
                context.set(key, first)
                merge_delta[key] = first
                continue
            conflicts[key] = branch_values

        if conflicts:
            context.set(f"{self.name}_conflicts", conflicts)
            merge_delta[f"{self.name}_conflicts"] = conflicts

        return merge_delta
