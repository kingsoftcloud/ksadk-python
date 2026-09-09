import asyncio
import json
import os
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from ksadk.plugins.providers.dsh_capabilities import DshMcpConnectorLease
from ksadk.resource_runtime.langgraph import create_bound_resource_tools
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from ksadk.studio.runtime_source import _langgraph_source
from tests.resource_runtime.test_worker_process import initialization
from tests.resource_runtime.test_worker_process import upstream as upstream


async def test_graph_consumes_real_scoped_core_and_closes_tools(upstream, monkeypatch):
    modules = os.environ.get("KSADK_TEST_DSH_NODE_MODULES")
    if not modules:
        pytest.skip("Installed official Cordis and DSH tools are required")
    endpoint, calls = upstream
    init = initialization(endpoint)
    supervisor = ResourceSupervisor("generation-a")
    node = None
    try:
        active = await supervisor.activate(init)
        root = Path(__file__).resolve().parents[2]
        bundles = root / "ksadk/plugins/providers/bundles"
        node = await asyncio.create_subprocess_exec(
            "node", str(root / "tests/fixtures/resource_runtime/node_mcp_host.mjs"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        node.stdin.write((json.dumps({
            "nodeModules": modules,
            "socketPath": str(active.socket_path),
            "profileDigest": init.scopes[0].profile_digest,
            "bridgePlugin": (bundles / "dsh-platform-resources/index.mjs").as_uri(),
            "knowledgePlugin": (bundles / "dsh-knowledge/index.mjs").as_uri(),
            "hostPlugin": (bundles / "ksadk-dsh-capability-host/index.mjs").as_uri(),
        }) + "\n").encode())
        await node.stdin.drain()
        token_line = await asyncio.wait_for(node.stdout.readline(), 10)
        ready_line = await asyncio.wait_for(node.stdout.readline(), 10)
        token_prefix = b"@@KSADK_DSH_CAPABILITY_TOKEN@@"
        ready_prefix = b"@@KSADK_DSH_CAPABILITY_READY@@"
        if not token_line.startswith(token_prefix) or not ready_line.startswith(ready_prefix):
            raise RuntimeError("Core fixture did not initialize")
        ready = json.loads(ready_line[len(ready_prefix):])
        connector = DshMcpConnectorLease(
            endpoint=ready["endpoint"], profile="studio",
            profile_digest=init.scopes[0].profile_digest,
            descriptor_digest="sha256:" + "d" * 64,
            _bearer_token=token_line[len(token_prefix):].decode().strip(),
        )
        async with create_bound_resource_tools(
            connector, active.leases, tool_aliases={"kb_search": "search_knowledge_base"},
        ) as tools:
            builder = StateGraph(MessagesState)
            builder.add_node("resources", ToolNode(list(tools)))
            builder.add_edge(START, "resources")
            builder.add_edge("resources", END)
            graph = builder.compile()

            async def call(query):
                result = await graph.ainvoke({"messages": [AIMessage(
                    content="", tool_calls=[{
                        "id": "graph-call", "name": "kb_search", "args": {"query": query},
                    }],
                )]})
                return result["messages"][-1]

            success = await call("hello")
            assert success.status == "success"
            assert "worker-result" in success.content
            assert success.artifact["structuredContent"]["items"][0]["documentId"] == "document-a"
            empty = await call("resource-empty")
            assert empty.status == "success"
            assert empty.artifact["structuredContent"]["status"] == "empty"
            failed = await call("resource-error")
            assert failed.status == "error"
            assert json.loads(failed.content)["structuredContent"]["status"] == "failed"
            assert len(calls) == 3
            serialized = json.dumps(success.model_dump(), default=str)
            assert "Bearer" not in serialized and "ks2." not in serialized

            # Exercise the Studio-generated model -> ToolNode -> model graph.
            # Only model output is scripted; MCP/Core/Worker/upstream stay real.
            namespace = {}
            exec(_langgraph_source("Use resources", "fixture", "agent", [], []), namespace)
            bound_names = []

            class Model:
                def __init__(self, **kwargs):
                    self.names = []

                def bind_tools(self, selected):
                    self.names = [tool.name for tool in selected]
                    bound_names.append(self.names)
                    return self

                def invoke(self, messages):
                    if isinstance(messages[-1], ToolMessage):
                        return AIMessage(content="verified " + messages[-1].content)
                    if not self.names:
                        return AIMessage(content="no resources")
                    return AIMessage(content="", tool_calls=[{
                        "id": "generated-call", "name": self.names[0],
                        "args": {"query": "generated"},
                    }])

            namespace["ChatOpenAI"] = Model
            monkeypatch.setenv("OPENAI_API_KEY", "fake-model-key")
            factory = namespace["ksadk_graph_factory"]
            saver = MemorySaver()
            generated = factory(checkpointer=saver, resource_tools=tools)
            independent = factory(checkpointer=None)
            with pytest.raises(ValueError, match="RESOURCE_TOOL_NAME_CONFLICT"):
                factory(checkpointer=None, resource_tools=tools + tools)
            config = {"configurable": {"thread_id": "generated-thread"}}
            result = await generated.ainvoke(
                {"messages": [HumanMessage(content="retrieve")]}, config=config,
            )
            assert "worker-result" in result["messages"][-1].content
            assert bound_names == [["kb_search"], ["kb_search"]]
            independent_result = await independent.ainvoke(
                {"messages": [HumanMessage(content="hello")]},
            )
            assert independent_result["messages"][-1].content == "no resources"
            assert generated.checkpointer is saver
            checkpoint = await generated.aget_state(config)
            checkpoint_text = json.dumps(checkpoint.values, default=str)
            assert "ks2." not in checkpoint_text and "fake-model-key" not in checkpoint_text
            assert len(calls) == 4
            await supervisor.deactivate(active.activation_id)
            revoked = await call("revoked")
            assert revoked.status == "error"
            assert len(calls) == 4
        closed = await call("closed")
        assert closed.status == "error" and closed.content == "RESOURCE_SESSION_CLOSED"
        assert len(calls) == 4
    finally:
        if node is not None and node.returncode is None:
            node.kill()
            await node.wait()
        await supervisor.aclose()
