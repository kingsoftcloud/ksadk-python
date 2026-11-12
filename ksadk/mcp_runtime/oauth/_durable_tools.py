"""Opt-in tool objects backed by native LangGraph tasks, not a custom ToolNode."""

import asyncio
from collections.abc import Mapping

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.config import get_config
from langgraph.func import task
from langgraph.runtime import get_runtime
from langgraph.types import Command
from pydantic import PrivateAttr

from .client import BrokerError


def saved_call(state, supplied):
    """Match any call in the saved model batch, not only the first pending one."""
    if not isinstance(supplied, Mapping):
        raise BrokerError("MCP_TOOL_CALL_NOT_PENDING")
    answered = set()
    for message in reversed(state.get("messages", [])):
        if isinstance(message, ToolMessage):
            answered.add(message.tool_call_id)
        elif isinstance(message, AIMessage):
            calls = [c for c in message.tool_calls if c.get("id") == supplied.get("id")]
            if len(calls) != 1 or supplied.get("id") in answered:
                break
            call = calls[0]
            # Native ToolNode may add InjectedState/ToolRuntime to ordinary
            # tools. The model-owned argument values must still be identical.
            args = supplied.get("args", {})
            if supplied.get("name") != call["name"] or any(
                key not in args or args[key] != value for key, value in call["args"].items()
            ):
                break
            return call
    raise BrokerError("MCP_TOOL_CALL_NOT_PENDING")


def _encode(result):
    # Command itself is not a JSON state-channel value in the SDK stream
    # contract. Its public fields and LangChain messages are supported values.
    if isinstance(result, Command):
        return {
            "kind": "command",
            "update": result.update,
            "goto": result.goto,
            "graph": result.graph,
            "resume": result.resume,
        }
    return {"kind": "result", "value": result}


def _decode(result):
    if result["kind"] == "command":
        return Command(**{k: result[k] for k in ("update", "goto", "graph", "resume")})
    return result["value"]


async def _saved_task_result(parent_config, task_config):
    """Read an exact native receipt when nested runners replay a finished task.

    LangGraph 1.2 can skip pending RETURN writes on a subgraph resume. Keep
    this compatibility adapter local to our tool objects: use the saver's
    public read API, never alter the graph, runner, scratchpad or saver writes.
    The binding keys below are LangGraph execution config, not user options.
    """
    parent = parent_config.get("configurable", {})
    current = task_config.get("configurable", {})
    saver = parent.get("__pregel_checkpointer")
    if saver is None:
        return None
    node_ns = parent.get("checkpoint_ns", "")
    graph_ns = node_ns.rpartition("|")[0]
    checkpoint_id = parent.get("checkpoint_map", {}).get(graph_ns)
    task_id = current.get("__pregel_task_id")
    if not checkpoint_id or not task_id:
        raise BrokerError("MCP_TOOL_CHECKPOINT_BINDING_MISSING")
    saved = await saver.aget_tuple(
        {
            "configurable": {
                "thread_id": parent["thread_id"],
                "checkpoint_ns": graph_ns,
                "checkpoint_id": checkpoint_id,
            }
        }
    )
    for tid, channel, value in (saved.pending_writes or []) if saved else []:
        if tid == task_id and channel == "__return__":
            if not isinstance(value, dict):
                raise BrokerError("MCP_TOOL_REPLAY_MISMATCH")
            return value
    return None


class _Batch:
    def __init__(self, state):
        self.state = state
        self.lock = asyncio.Lock()
        self.aborted = None
        self.stopped = False

    async def invoke(self, original, supplied, config, kwargs):
        call = saved_call(self.state, supplied)
        identity = {key: call[key] for key in ("id", "name", "args")}
        async with self.lock:
            # Native ToolNode schedules its calls in model order. Serialize
            # this opt-in batch so later calls cannot escape a consent/rejection
            # boundary. No existing tool object or ToolNode class is modified.
            if self.aborted is not None:
                raise self.aborted
            if self.stopped:
                return ToolMessage(
                    content='{"error":"MCP_OPERATION_STOPPED"}',
                    name=call["name"],
                    tool_call_id=call["id"],
                    status="error",
                )

            fresh_result = None
            parent_config = config

            async def invoke(saved_identity):
                nonlocal fresh_result
                task_config = get_config()
                cached = await _saved_task_result(parent_config, task_config)
                if cached is not None:
                    fresh_result = cached
                    return cached
                # Use the task's context for interrupt(), not the parent node's
                # scratchpad. Neither tools, credentials nor injected runtime
                # objects are task arguments or checkpointed values.
                result = await original.ainvoke(supplied, config=task_config, **kwargs)
                fresh_result = {"call": saved_identity, "result": _encode(result)}
                return fresh_result

            try:
                try:
                    runtime = get_runtime()
                except (RuntimeError, KeyError):
                    runtime = None
                if runtime is None:
                    # Preserve direct async tool invocation outside a graph;
                    # real OAuth still requires its existing trusted extension.
                    result = await original.ainvoke(supplied, config=config, **kwargs)
                else:
                    stored = await task(invoke, name="ksadk_mcp_tool_call")(identity)
                    # LangGraph 1.2's nested streaming runner persists RETURN
                    # but can resolve the fresh task future with None. Reuse
                    # only the value from this successfully awaited invocation;
                    # cached/cold results still come from the native receipt.
                    # Never rerun a tool to recover a missing task result.
                    if stored is None and fresh_result is not None:
                        stored = fresh_result
                    if not isinstance(stored, dict) or stored.get("call") != identity:
                        raise BrokerError("MCP_TOOL_REPLAY_MISMATCH")
                    result = _decode(stored["result"])
                if isinstance(result, Command) and isinstance(result.update, dict):
                    self.stopped = bool(result.update.get("mcp_stop"))
                return result
            except BaseException as error:
                # Preserve GraphInterrupt and cancellation; prevent siblings
                # waiting on the lock from dispatching after this boundary.
                self.aborted = error
                raise


class _DurableTool(BaseTool):
    _original: BaseTool = PrivateAttr()
    _batch: _Batch = PrivateAttr()

    def __init__(self, original, batch):
        super().__init__(
            name=original.name,
            description=original.description,
            args_schema=original.args_schema or original.get_input_schema(),
            return_direct=original.return_direct,
            response_format=original.response_format,
        )
        self._original, self._batch = original, batch

    @property
    def func(self):
        return getattr(self._original, "func", None)

    @property
    def coroutine(self):
        return getattr(self._original, "coroutine", None)

    def get_input_schema(self, config=None):
        return self._original.get_input_schema(config)

    def _run(self, *args, **kwargs):
        raise NotImplementedError("Durable MCP tools require async ainvoke")

    async def ainvoke(self, input, config=None, **kwargs):
        return await self._batch.invoke(self._original, input, config, kwargs)


def durable_tools(tools, state):
    if any(not isinstance(tool, BaseTool) for tool in tools):
        raise TypeError("tools must contain LangChain tool objects")
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise BrokerError("MCP_TOOL_NAME_COLLISION")
    batch = _Batch(state)
    return [_DurableTool(tool, batch) for tool in tools]
