"""Runner 适配器。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from ksadk.agents.base import BaseOrchestrationAgent
from ksadk.agents.context import OrchestrationContext
from ksadk.agents.event import AgentEvent


class RunnerAgent(BaseOrchestrationAgent):
    """将 runner-like 对象包装为可编排子 agent。"""

    def __init__(self, name: str, runner: Any, description: str = ""):
        if not self._is_runner_like(runner):
            raise TypeError("RunnerAgent requires a runner-like object with an invoke method")
        super().__init__(name=name, sub_agents=None, description=description)
        self.runner = runner

    async def _run_impl(self, context: OrchestrationContext) -> AsyncGenerator[AgentEvent, None]:
        async for event in self._run_runner_like(self.name, self.runner, context):
            yield event
