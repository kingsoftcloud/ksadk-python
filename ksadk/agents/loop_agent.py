"""循环编排 agent。"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Callable
from typing import Any

from ksadk.agents.base import BaseOrchestrationAgent
from ksadk.agents.context import OrchestrationContext
from ksadk.agents.event import AgentEvent, EventType


class LoopAgent(BaseOrchestrationAgent):
    """重复执行子 agent，直到命中退出条件。"""

    def __init__(
        self,
        name: str,
        sub_agents: list[Any] | None = None,
        max_iterations: int | None = None,
        exit_condition: Callable[[OrchestrationContext], Any] | None = None,
        **kwargs: Any,
    ):
        if max_iterations is not None and max_iterations < 0:
            raise ValueError("max_iterations must be non-negative")
        super().__init__(name=name, sub_agents=sub_agents, **kwargs)
        self.max_iterations = max_iterations
        self.exit_condition = exit_condition

    async def _run_impl(self, context: OrchestrationContext) -> AsyncGenerator[AgentEvent, None]:
        iteration = 0

        while self.max_iterations is None or iteration < self.max_iterations:
            metadata = {"iteration": iteration}
            yield AgentEvent(
                agent_name=self.name,
                event_type=EventType.AGENT_START,
                branch=context.branch,
                metadata=metadata,
                data=metadata,
            )

            should_exit = False
            for sub_agent in self.sub_agents:
                async for event in self._run_sub_agent(sub_agent, context):
                    yield event
                    if event.escalate or event.event_type == EventType.ESCALATE:
                        should_exit = True
                        break
                if should_exit:
                    break

            yield AgentEvent(
                agent_name=self.name,
                event_type=EventType.AGENT_END,
                branch=context.branch,
            )

            iteration += 1
            if should_exit:
                break

            if self.exit_condition is None:
                continue

            result = self.exit_condition(context)
            if inspect.isawaitable(result):
                result = await result
            if result:
                break
