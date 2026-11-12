"""CLI-created managed A2A acceptance graph. No endpoints or credentials in code."""
import json
import os
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from ksadk.toolsets.a2a import get_a2a_tools

ROLE = "A"


def request_payload(state):
    message = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
    payload = json.loads(message.content)
    if not isinstance(payload, dict) or not isinstance(payload.get("path", []), list):
        raise ValueError("expected JSON object with path list and text")
    return payload


def prepare_call(state):
    payload = request_payload(state)
    path = payload.get("path", [])
    if not path:
        text = payload.get("text", "")
        if payload.get("request_input"):
            from langgraph.types import interrupt
            answer = interrupt({"kind": "structured_input", "prompt": "Provide the acceptance answer", "schema": {
                "type": "object", "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            }})
            text = f"{text}:{answer['answer']}"
        return {"messages": [AIMessage(content=f"{ROLE}:{text}")]}
    remaining = {**payload, "path": path[1:]}
    mode = os.getenv("A2A_EXAMPLE_MODE", "dynamic")
    name = "delegate" if mode == "handoff" else "call_a2a_agent"
    args = {"message": json.dumps(remaining, ensure_ascii=False)}
    if mode != "handoff":
        args["agent"] = path[0]
    return {"messages": [AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": "call-" + uuid4().hex}
    ])]}


async def finish(state):
    payload = request_payload(state)
    last = state["messages"][-1]
    if isinstance(last, ToolMessage):
        return {"messages": [AIMessage(content=f"{ROLE}:{last.content}")]}
    if not payload.get("path") and payload.get("mcp"):
        import asyncio
        import sys
        from pathlib import Path
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        server = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).with_name("mcp_server.py"))])
        async with asyncio.timeout(20):
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("echo", {"text": payload.get("text", "")})
                    if result.isError:
                        raise RuntimeError("acceptance MCP echo failed")
                    text = "".join(part.text for part in result.content if part.type == "text")
        return {"messages": [AIMessage(content=f"{ROLE}:{text}")]}
    if not payload.get("path") and payload.get("model"):
        from langchain_openai import ChatOpenAI
        model = ChatOpenAI(model=os.environ["OPENAI_MODEL_NAME"],
                           base_url=os.getenv("OPENAI_BASE_URL"),
                           api_key=os.environ["OPENAI_API_KEY"], timeout=60)
        answer = await model.ainvoke([HumanMessage(content=payload.get("text", "Say ready"))])
        return {"messages": [answer]}
    return {"messages": []}


def fixed_spec():
    from ksadk.a2a.invocation_types import RemoteAgentSpec
    return RemoteAgentSpec(local_name="delegate", binding_id="acceptance-" + ROLE,
        space_id=os.environ["A2A_EXAMPLE_SPACE_CANONICAL_ID"],
        target_agent_id=os.environ["A2A_EXAMPLE_TARGET_ID"],
        expected_version_id=os.environ["A2A_EXAMPLE_TARGET_VERSION_ID"], exposure="tool")


def ksadk_graph_factory(*, checkpointer, resource_tools=()):
    mode = os.getenv("A2A_EXAMPLE_MODE", "dynamic")
    graph = StateGraph(MessagesState)
    if mode == "node":
        from ksadk.a2a.langgraph import remote_node
        node = remote_node(fixed_spec().model_copy(update={"exposure": "native"}))

        async def invoke_remote(state, config: RunnableConfig):
            payload = request_payload(state)
            remaining = {**payload, "path": payload.get("path", [])[1:]}
            # The example protocol carries only this turn's remaining hops.
            # Keep the parent history in its own checkpoint, not in remote JSON.
            return await node({"messages": [HumanMessage(
                content=json.dumps(remaining, ensure_ascii=False))]}, config)

        invoke_remote.remote_spec = node.remote_spec
        graph.add_node("remote", invoke_remote)
        graph.add_edge(START, "remote")
        graph.add_edge("remote", END)
    else:
        if mode == "handoff":
            from ksadk.a2a.langgraph import handoff_tool
            tools = [handoff_tool(fixed_spec())]
        elif mode == "dynamic":
            tools = get_a2a_tools()
        else:
            raise ValueError("unsupported A2A_EXAMPLE_MODE")
        async def prepare(state):
            payload = request_payload(state)
            if not payload.get("model_route") or not payload.get("path"):
                return prepare_call(state)
            from langchain_openai import ChatOpenAI
            from langchain_core.messages import SystemMessage
            name = "delegate" if mode == "handoff" else "call_a2a_agent"
            remaining = {**payload, "path": payload["path"][1:]}
            arguments = {"message": json.dumps(remaining, ensure_ascii=False)}
            if mode != "handoff":
                arguments["agent"] = payload["path"][0]
            model = ChatOpenAI(model=os.environ["OPENAI_MODEL_NAME"],
                base_url=os.getenv("OPENAI_BASE_URL"), api_key=os.environ["OPENAI_API_KEY"], timeout=60)
            answer = await model.bind_tools(tools, tool_choice=name).ainvoke([
                SystemMessage(content="Call the named tool exactly once using the supplied arguments. Preserve the JSON message."),
                HumanMessage(content=json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False)),
            ])
            if len(answer.tool_calls) != 1 or answer.tool_calls[0]["name"] != name:
                raise RuntimeError("acceptance model did not select the expected tool")
            actual = answer.tool_calls[0]["args"]
            if (json.loads(actual.get("message", "null")) != remaining
                    or (mode != "handoff" and actual.get("agent") != arguments["agent"])):
                raise RuntimeError("acceptance model changed the required route or payload")
            return {"messages": [answer]}

        graph.add_node("prepare", prepare)
        graph.add_node("tools", ToolNode([*tools, *resource_tools]))
        graph.add_node("finish", finish)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "tools")
        graph.add_edge("tools", "finish")
        graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def ksadk_prepare_state(payload, session_context):
    return {"messages": [HumanMessage(content=str(payload.get("input") or "{}"))]}


root_agent = ksadk_graph_factory(checkpointer=MemorySaver())
