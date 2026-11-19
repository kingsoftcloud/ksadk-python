"""LangChain create_agent middleware for dynamically registered tunnel tools."""

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware


class _TunnelState(AgentState):
    tunnel_catalog: list[dict]


class TunnelMiddleware(AgentMiddleware):
    state_schema = _TunnelState

    def __init__(self, tunnel_tools):
        self.tunnel_tools = tunnel_tools

    async def abefore_model(self, state, runtime):
        return {"tunnel_catalog": await self.tunnel_tools.discover()}

    async def awrap_model_call(self, request, handler):
        tools = await self.tunnel_tools.get_tools(request.state["tunnel_catalog"])
        return await handler(request.override(tools=[*request.tools, *tools]))

    async def awrap_tool_call(self, request, handler):
        for tool in await self.tunnel_tools.get_tools(request.state["tunnel_catalog"]):
            if tool.name == request.tool_call["name"]:
                return await handler(request.override(tool=tool))
        return await handler(request)
