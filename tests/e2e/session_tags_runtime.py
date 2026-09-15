"""Local HTTP acceptance fixture: real frameworks, deterministic tool-calling models.

Run with this checkout's Python: session_tags_runtime.py FRAMEWORK PORT.
The Agent's business code intentionally returns its tag snapshot as test output.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import uvicorn

from ksadk.runtime_context import get_current_session_context, get_current_session_tags
from ksadk.session_context import SessionContext


async def read_tags() -> str:
    """Read this invocation's session classification."""
    before = dict(get_current_session_tags())
    await asyncio.sleep(0.15)
    assert before == dict(get_current_session_tags()), "snapshot changed during invocation"
    return json.dumps(
        {"tags": before, "revision": get_current_session_context().revision}, sort_keys=True
    )


@dataclass
class TaggedContext:
    session: SessionContext


def build_runner(kind, *, interrupting=False):
    detection = SimpleNamespace(name=kind, type=SimpleNamespace(value=kind))
    if kind in {"langgraph", "langchain", "legacy"}:
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
        from langgraph.graph import END, START, MessagesState, StateGraph
        from langgraph.runtime import Runtime
        from ksadk.runners.langchain_runner import LangChainRunner
        from ksadk.runners.langgraph_runner import LangGraphRunner

        class ToolModel(BaseChatModel):
            @property
            def _llm_type(self):
                return "session-tags-fixture"

            def bind_tools(self, tools, **kwargs):
                return self

            def message(self, messages):
                if isinstance(messages[-1], ToolMessage):
                    return AIMessage(content=messages[-1].content)
                return AIMessage(
                    content="", tool_calls=[{"name": "read_tags", "args": {}, "id": "read"}]
                )

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                return ChatResult(generations=[ChatGeneration(message=self.message(messages))])

            def _stream(self, messages, stop=None, run_manager=None, **kwargs):
                msg = self.message(messages)
                if msg.tool_calls:
                    chunk = AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {"name": "read_tags", "args": "{}", "id": "read", "index": 0}
                        ],
                    )
                else:
                    chunk = AIMessageChunk(content=msg.content)
                yield ChatGenerationChunk(message=chunk)

        if kind == "langchain":
            from langchain.agents import create_agent

            runner = LangChainRunner(detection, ".")
            from langchain.agents.middleware import HumanInTheLoopMiddleware

            runner._agent = create_agent(
                ToolModel(),
                tools=[read_tags],
                middleware=(
                    [HumanInTheLoopMiddleware(interrupt_on={"read_tags": True})]
                    if interrupting
                    else []
                ),
            )
        else:
            runner = LangGraphRunner(detection, ".")

            async def respond(state: MessagesState, runtime: Runtime[TaggedContext]):
                assert dict(runtime.context.session.tags) == dict(get_current_session_tags())
                if interrupting:
                    from langgraph.types import interrupt

                    interrupt({"prompt": "Continue reading tags?"})
                return {"messages": [AIMessage(content=await read_tags())]}

            graph = StateGraph(MessagesState, context_schema=TaggedContext)
            graph.add_node("respond", respond)
            graph.add_edge(START, "respond")
            graph.add_edge("respond", END)
            runner._agent = graph.compile()
            if kind == "legacy":
                # A historical Agent with no helper imports or context schema.
                async def old_agent(state):
                    return {"messages": [AIMessage(content="legacy-agent-ok")]}

                old_graph = StateGraph(MessagesState)
                old_graph.add_node("respond", old_agent)
                old_graph.add_edge(START, "respond")
                old_graph.add_edge("respond", END)
                runner._agent = old_graph.compile()
    elif kind == "adk":
        from google.adk.agents import Agent
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types
        from ksadk.runners.adk_runner import ADKRunner

        class ToolModel(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                last = llm_request.contents[-1]
                result = next(
                    (p.function_response for p in last.parts if p.function_response), None
                )
                part = (
                    types.Part(text=result.response["result"])
                    if result
                    else types.Part(function_call=types.FunctionCall(name="read_tags", args={}))
                )
                content = types.Content(role="model", parts=[part])
                if result and stream:
                    yield LlmResponse(content=content, partial=True)
                yield LlmResponse(content=content, partial=False)

        runner = ADKRunner(detection, ".")
        runner._agent = Agent(
            name="tag_agent", model=ToolModel(model="session-tags-fixture"), tools=[read_tags]
        )
        runner._session_service = InMemorySessionService()
        runner._runner = Runner(
            agent=runner._agent, app_name="tag_agent", session_service=runner._session_service
        )
    else:
        raise ValueError(kind)
    runner._ksadk_runtime_loaded = True
    return runner


def build_app(kind):
    from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext, RuntimeRegistry
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter, LangGraphRuntimeAdapter
    from ksadk.server.composition import configure_runtime_app
    from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
    from ksadk.sessions.in_memory import InMemorySessionService

    runner = build_runner(kind)
    runtime_type = "langgraph" if kind in {"langchain", "legacy"} else kind
    context = RuntimeLaunchContext(
        runtime_type=runtime_type, project_dir=".", detection=runner.detection_result
    )
    registry = RuntimeRegistry()
    adapter = ADKRuntimeAdapter if kind == "adk" else LangGraphRuntimeAdapter
    registry.register(runtime_type, lambda _: adapter(runner))
    sessions = InMemorySessionService()
    return create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(registry),
            launch_context=context,
            session_service_provider=lambda: sessions,
            session_backend_provider=lambda: {
                "backend": "memory",
                "durable": False,
                "sharedAcrossPods": False,
            },
        ),
        configure_runtime_app,
    )


if __name__ == "__main__":
    uvicorn.run(
        build_app(sys.argv[1]), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning"
    )
