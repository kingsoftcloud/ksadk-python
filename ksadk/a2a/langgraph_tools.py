"""Preserve native LangChain tool identity while routing managed A2A calls."""

import json
from contextvars import ContextVar

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

_native_call: ContextVar[str | None] = ContextVar("langgraph_a2a_native_tool_call", default=None)


def native_tool_call_id():
    call_id = _native_call.get()
    if not call_id:
        raise ValueError("A2A_NATIVE_TOOL_TRIGGER_REQUIRED")
    return call_id


class ManagedLangGraphTool(StructuredTool):
    async def ainvoke(self, input, config=None, **kwargs):
        call_id = input.get("id") if isinstance(input, dict) and input.get("type") == "tool_call" else None
        token = _native_call.set(call_id)
        try:
            # StructuredTool sends sync-only discovery straight to invoke in
            # an executor, bypassing our managed _arun. BaseTool keeps the
            # async dispatch; the fallback _arun still offloads sync work.
            output = await BaseTool.ainvoke(self, input, config, **kwargs)
            from langchain_core.messages import ToolMessage

            if isinstance(output, ToolMessage) and isinstance(output.content, str):
                try:
                    result = json.loads(output.content)
                except ValueError:
                    result = None
                if (isinstance(result, dict) and result.get("ok") is False
                        and result.get("error_type")):
                    output = output.model_copy(update={"status": "error"})
            return output
        finally:
            _native_call.reset(token)

    async def _arun(self, *args, config: RunnableConfig, run_manager=None, **kwargs):
        from ksadk.a2a.langgraph_managed import _binding
        from ksadk.toolsets.a2a import call_a2a_agent, get_a2a_agent_card, list_a2a_agents

        binding = _binding.get()
        runtime = binding.runtime if binding else None
        function = self.coroutine or self.func
        if runtime is not None:
            if function is call_a2a_agent:
                return await runtime.call(kwargs, config)
            if function in (list_a2a_agents, get_a2a_agent_card):
                try:
                    if function is get_a2a_agent_card:
                        agent = kwargs.get("agent")
                        if not isinstance(agent, str) or not agent.strip():
                            raise ValueError("A2A_AGENT_REQUIRED")
                        return await runtime.discover(agent)
                    return await runtime.discover()
                except Exception as exc:
                    code = str(exc)
                    if not code.startswith("A2A_") or len(code) >= 128:
                        code = "A2A_DISCOVERY_FAILED"
                    return {"ok": False, "error_type": code,
                            "error_message": "Managed A2A discovery failed"}
        return await super()._arun(*args, config=config, run_manager=run_manager, **kwargs)


def managed_tool(func):
    import inspect

    return ManagedLangGraphTool.from_function(
        func=None if inspect.iscoroutinefunction(func) else func,
        coroutine=func if inspect.iscoroutinefunction(func) else None,
    )
