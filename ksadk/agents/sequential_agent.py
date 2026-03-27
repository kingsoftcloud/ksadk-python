"""顺序编排 agent。"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from ksadk.agents.base import BaseOrchestrationAgent
from ksadk.agents.context import OrchestrationContext
from ksadk.agents.event import AgentEvent, EventType


class SequentialAgent(BaseOrchestrationAgent):
    """按序依次执行子 agent。"""

    async def _run_impl(self, context: OrchestrationContext) -> AsyncGenerator[AgentEvent, None]:
        for index, sub_agent in enumerate(self.sub_agents):
            agent_name = self._get_agent_name(sub_agent)
            metadata = {"sub_agent": agent_name, "index": index}
            yield AgentEvent(
                agent_name=self.name,
                event_type=EventType.AGENT_START,
                branch=context.branch,
                metadata=metadata,
                data=metadata,
            )
            async for event in self._run_sub_agent(sub_agent, context):
                yield event
            yield AgentEvent(
                agent_name=self.name,
                event_type=EventType.AGENT_END,
                branch=context.branch,
                metadata=metadata,
                data=metadata,
            )
