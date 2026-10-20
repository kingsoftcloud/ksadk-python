"""Managed discovery and dynamic calls tied to real LangGraph tool execution."""

import asyncio

from ksadk.a2a.exposure import compile_exposure_plan
from ksadk.a2a.invocation_types import RemoteAgentSpec
from ksadk.a2a.langgraph_managed import _invoke
from ksadk.a2a.langgraph_tools import native_tool_call_id
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.events.content import ToolCallContent
from ksadk.events.identity import stable_item_id


def graph_exposure_plan(graph):
    pending, seen, specs = [graph], set(), {}
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        spec = getattr(value, "remote_spec", None)
        metadata = getattr(value, "metadata", None)
        if spec is None and isinstance(metadata, dict) and metadata.get("ksadk_a2a_remote_spec"):
            spec = RemoteAgentSpec.model_validate(metadata["ksadk_a2a_remote_spec"])
        if spec is not None:
            previous = specs.get(spec.binding_id)
            if previous is not None and previous != spec:
                raise ValueError("A2A_BINDING_ID_CONFLICT")
            specs[spec.binding_id] = spec
        for attr in ("bound", "func", "afunc", "__self__"):
            child = getattr(value, attr, None)
            if child is not None:
                pending.append(child)
        for attr in ("nodes", "tools_by_name"):
            children = getattr(value, attr, None)
            if isinstance(children, dict):
                pending.extend(children.values())
        children = getattr(value, "steps", None)
        if isinstance(children, (list, tuple)):
            pending.extend(children)
    return compile_exposure_plan(list(specs.values()), framework="langgraph", consumer_id="graph")


class ManagedLangGraphTools:
    def __init__(self, *, bootstrap, authority, plan):
        self.bootstrap, self.authority, self.plan = bootstrap, authority, plan
        self.changed = asyncio.Condition()

    async def notify_event(self):
        async with self.changed:
            self.changed.notify_all()

    async def discover(self, agent_ref=None):
        from ksadk.toolsets.a2a import _a2a_space_id, _normalize_agent_ref

        self.authority.require_session(self.authority.session_id)
        space = _a2a_space_id()
        if not space:
            raise ValueError("A2A_SPACE_REQUIRED")
        discovered = await self.bootstrap.client_for_space(space).discover()
        agents = [{"agent_id": a.agent_id, "name": a.agent_card.name,
                   "description": getattr(a.agent_card, "description", ""),
                   "version_id": a.version_id} for a in discovered]
        if agent_ref is None:
            return {"ok": True, "space_id": space, "agents": agents}
        target = _normalize_agent_ref(str(agent_ref))
        matches = [a for a in agents if target and target in {
            _normalize_agent_ref(a["agent_id"]), _normalize_agent_ref(a["name"])}]
        if len(matches) != 1:
            raise ValueError("A2A_AGENT_NOT_FOUND" if not matches else "A2A_AGENT_AMBIGUOUS")
        return {"ok": True, "space_id": space, "agent": matches[0]}

    async def call(self, args, config):
        try:
            native_tool_call_id()
            message = args.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("A2A_MESSAGE_REQUIRED")
            if not isinstance(args.get("agent"), str) or not args["agent"].strip():
                raise ValueError("A2A_AGENT_REQUIRED")
            found = await self.discover(args.get("agent"))
            agent, space = found["agent"], found["space_id"]
            self.plan.assert_generic_target_allowed(agent["agent_id"])
            spec = RemoteAgentSpec(
                local_name="call_a2a_agent",
                binding_id=stable_item_id("a2a", space, agent["agent_id"]),
                space_id=space, target_agent_id=agent["agent_id"],
                expected_version_id=agent["version_id"], exposure="tool",
            )
            return await _invoke(spec, {"message": message}, config,
                                 surface="tool", result_envelope=True)
        except Exception as exc:
            from ksadk.a2a.tool_outcomes import tool_error_result

            return tool_error_result(exc)

    async def tool_trigger(self, spec, config):
        call_id = native_tool_call_id()
        namespace = config["metadata"]["langgraph_checkpoint_ns"]
        components = namespace.split("|") if namespace else []
        async def resolve():
            async with self.changed:
                while True:
                    facts = await self.authority.list(self.authority.session_id, item_kinds=["tool_call"])
                    # v3 message facts carry the graph namespace, while the
                    # running ToolNode config adds its own node:task segment.
                    # Some supported ToolNodes emit no tools-channel events;
                    # the real ainvoke ToolCall envelope plus the persisted
                    # model call is the proof, not a fabricated tool-start.
                    calls = {}
                    for event in facts:
                        if event.source.framework != "langgraph":
                            continue
                        if event.source.metadata.get("namespace") != components[:-1]:
                            continue
                        snapshot = getattr(event, "snapshot", None) or getattr(event, "initial", None)
                        if snapshot and any(isinstance(p, ToolCallContent)
                                            and p.call_id == call_id and p.name == spec.local_name
                                            for p in snapshot.parts):
                            calls[(event.scope_id, event.item_id)] = event
                    if len(calls) == 1:
                        event = next(iter(calls.values()))
                        return ScopeTriggerRef(scope_id=event.scope_id,
                                               item_id=event.item_id, call_id=call_id)
                    if len(calls) > 1:
                        raise ValueError("A2A_NATIVE_TRIGGER_AMBIGUOUS")
                    await self.changed.wait()
        try:
            from ksadk.a2a.timeouts import seconds

            return await asyncio.wait_for(
                resolve(), timeout=seconds("KSADK_A2A_NATIVE_TRIGGER_SECONDS", 15.0))
        except TimeoutError as exc:
            raise ValueError("A2A_NATIVE_TRIGGER_NOT_PERSISTED") from exc
