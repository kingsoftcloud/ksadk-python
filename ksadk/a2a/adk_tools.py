"""Managed ADK tool execution with native call identity and run-owned events."""

import asyncio
from contextlib import aclosing, contextmanager
from contextvars import ContextVar

from google.adk.tools.function_tool import FunctionTool

from ksadk.a2a.adk import _project
from ksadk.a2a.adk_invocation_context import ADKInvocationContextProvider, _path
from ksadk.a2a.exposure import compile_exposure_plan
from ksadk.a2a.invocation_types import RemoteAgentSpec
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.events.identity import stable_item_id, stable_scope_id

_binding = ContextVar("managed_adk_tools", default=None)


def native_tool_trigger(tool_context, tool_name="call_a2a_agent"):
    ctx = tool_context._invocation_context
    call_id = tool_context.function_call_id
    if not call_id:
        raise ValueError("A2A_NATIVE_TRIGGER_ID_REQUIRED")
    for event in reversed(ctx.session.events):
        if (
            event.invocation_id != ctx.invocation_id
            or event.author != ctx.agent.name
            or event.branch != ctx.branch
        ):
            continue
        calls = [
            part.function_call
            for part in (getattr(event.content, "parts", None) or ())
            if part.function_call is not None and part.function_call.id == call_id
        ]
        if not calls:
            continue
        if len(calls) != 1 or calls[0].name != tool_name or not event.id:
            raise ValueError("A2A_NATIVE_TRIGGER_AMBIGUOUS")
        path = _path(event)
        return ScopeTriggerRef(
            scope_id=stable_scope_id("adk", event.invocation_id, path),
            item_id=stable_item_id(
                "adk", event.invocation_id, path, event.id, call_id, "tool_call"
            ),
            call_id=call_id,
        )
    raise ValueError("A2A_NATIVE_TRIGGER_CALL_MISSING")


class ManagedA2ACallTool(FunctionTool):
    def __init__(self):
        from ksadk.toolsets.a2a import call_a2a_agent

        super().__init__(call_a2a_agent)

    async def run_async(self, *, args, tool_context):
        runtime = _binding.get()
        if runtime is None:
            return {"ok": False, "error_type": "A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED"}
        try:
            spec = getattr(self, "remote_spec", None)
            if spec is not None:
                return await runtime.call(args, tool_context, fixed_spec=spec)
            return await runtime.call(args, tool_context)
        except Exception as exc:
            from ksadk.a2a.tool_outcomes import tool_error_result

            return tool_error_result(exc)


class ManagedA2AHandoffTool(ManagedA2ACallTool):
    """A fixed remote expert; the model supplies only the message."""

    def __init__(self, spec):
        spec = RemoteAgentSpec.model_validate(spec.model_dump())
        compile_exposure_plan([spec], framework="adk", consumer_id=spec.local_name)

        async def invoke(message: str):
            raise RuntimeError("A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED")

        invoke.__name__ = spec.local_name
        invoke.__doc__ = spec.description or "Delegate to this fixed remote expert and receive its result."
        FunctionTool.__init__(self, invoke)
        self.remote_spec = spec


def adk_exposure_plan(root):
    pending, seen, specs = [root], set(), []
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        spec = getattr(node, "remote_spec", None)
        if spec is not None:
            specs.append(spec)
        pending.extend(getattr(node, "sub_agents", ()) or ())
        pending.extend(getattr(node, "tools", ()) or ())
    if len({spec.target_agent_id for spec in specs}) != len(specs):
        raise ValueError("A2A_EXPOSURE_CONFLICT")
    return compile_exposure_plan(specs, framework="adk", consumer_id=root.name)


class ManagedA2ADiscoveryTool(FunctionTool):
    def __init__(self, func):
        super().__init__(func)
        self._detail = func.__name__ == "get_a2a_agent_card"

    async def run_async(self, *, args, tool_context):
        runtime = _binding.get()
        if runtime is None:
            return {"ok": False, "error_type": "A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED"}
        try:
            if self._detail and (
                not isinstance(args.get("agent"), str) or not args["agent"].strip()
            ):
                raise ValueError("A2A_AGENT_REQUIRED")
            return await runtime.discover(args.get("agent") if self._detail else None)
        except Exception as exc:
            code = str(exc)
            if not code.startswith("A2A_") or len(code) >= 128:
                code = "A2A_DISCOVERY_FAILED"
            return {
                "ok": False,
                "error_type": code,
                "error_message": "Managed A2A discovery failed",
            }


def install_managed_tools(root):
    """Replace only the SDK function, never an unrelated tool with the same name."""
    from ksadk.toolsets.a2a import call_a2a_agent, get_a2a_agent_card, list_a2a_agents

    pending, seen = [root], set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if hasattr(node, "tools"):
            replaced = []
            for tool in node.tools:
                func = getattr(tool, "func", None) or getattr(tool, "coroutine", None) or tool
                if func is call_a2a_agent:
                    tool = ManagedA2ACallTool()
                elif func is list_a2a_agents or func is get_a2a_agent_card:
                    tool = ManagedA2ADiscoveryTool(func)
                replaced.append(tool)
            node.tools = replaced
        pending.extend(getattr(node, "sub_agents", ()) or ())


class ManagedADKTools:
    def __init__(self, *, bootstrap, authority, services, emit):
        self.bootstrap, self.authority = bootstrap, authority
        self.services, self.emit = services, emit

    async def discover(self, agent_ref=None):
        from ksadk.toolsets.a2a import _a2a_space_id, _normalize_agent_ref

        self.authority.require_session(self.authority.session_id)
        space = _a2a_space_id()
        if not space:
            raise ValueError("A2A_SPACE_REQUIRED")
        discovered = await self.bootstrap.client_for_space(space).discover()
        # Only public discovery metadata is returned to the model. Connection
        # origins, credentials and prepared routes belong to the runtime client.
        agents = [
            {
                "agent_id": agent.agent_id,
                "name": agent.agent_card.name,
                "description": getattr(agent.agent_card, "description", ""),
                "version_id": agent.version_id,
                "card_sha256": getattr(agent, "card_sha256", ""),
                "source": getattr(agent, "source", "hosted"),
            }
            for agent in discovered
        ]
        if agent_ref is None:
            return {"ok": True, "space_id": space, "agents": agents}
        target = _normalize_agent_ref(str(agent_ref))
        matches = [
            agent
            for agent in agents
            if target
            and target
            in {_normalize_agent_ref(agent["agent_id"]), _normalize_agent_ref(agent["name"])}
        ]
        if len(matches) != 1:
            raise ValueError("A2A_AGENT_NOT_FOUND" if not matches else "A2A_AGENT_AMBIGUOUS")
        return {"ok": True, "agent": matches[0]}

    async def call(self, args, tool_context, *, fixed_spec=None):
        from ksadk.a2a.runtime_input import invoke_with_input
        from ksadk.events.content import TextContent, ToolCallContent
        from ksadk.toolsets.a2a import _a2a_space_id, _normalize_agent_ref

        message = args.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("A2A_MESSAGE_REQUIRED")
        tool_name = fixed_spec.local_name if fixed_spec is not None else "call_a2a_agent"
        trigger = native_tool_trigger(tool_context, tool_name)
        ctx = tool_context._invocation_context
        self.authority.require_session(self.authority.session_id)
        # Native session IDs may be mapped from the public session. Bind the
        # actual native call to its already-persisted public fact instead of
        # assuming those session strings are equal (or trusting another run's
        # ToolContext merely because its call_id has the same spelling).
        facts = await self.authority.list(self.authority.session_id)
        bound = False
        for event in facts:
            if (
                event.scope_id != trigger.scope_id
                or getattr(event, "item_id", None) != trigger.item_id
            ):
                continue
            snapshot = getattr(event, "initial", None) or getattr(event, "snapshot", None)
            if snapshot is not None and any(
                isinstance(part, ToolCallContent)
                and part.call_id == trigger.call_id
                and part.name == tool_name
                for part in snapshot.parts
            ):
                bound = True
                break
        if not bound:
            raise ValueError("A2A_NATIVE_TRIGGER_NOT_PERSISTED")
        if fixed_spec is not None:
            spec = RemoteAgentSpec.model_validate(fixed_spec.model_dump())
            if set(args) != {"message"}:
                raise ValueError("A2A_FIXED_TARGET_ARGUMENT_INVALID")
            declarations = [tool.remote_spec for tool in ctx.agent.tools
                            if isinstance(tool, ManagedA2AHandoffTool) and tool.name == tool_name]
            if declarations != [spec]:
                raise ValueError("A2A_BINDING_MISMATCH")
            adk_exposure_plan(ctx.agent)
        else:
            space = _a2a_space_id()
            if not space:
                raise ValueError("A2A_SPACE_REQUIRED")
            discovered = await self.bootstrap.client_for_space(space).discover()
            target = _normalize_agent_ref(str(args.get("agent") or ""))
            matches = [
                agent
                for agent in discovered
                if target
                in {_normalize_agent_ref(agent.agent_id), _normalize_agent_ref(agent.agent_card.name)}
            ]
            if len(matches) != 1:
                raise ValueError("A2A_AGENT_NOT_FOUND" if not matches else "A2A_AGENT_AMBIGUOUS")
            agent = matches[0]
            plan = adk_exposure_plan(ctx.agent)
            if any(isinstance(tool, ManagedA2AHandoffTool) and tool.remote_spec.target_agent_id == agent.agent_id
                   for tool in ctx.agent.tools):
                raise ValueError("A2A_EXPOSURE_CONFLICT")
            plan.assert_generic_target_allowed(agent.agent_id)
            spec = RemoteAgentSpec(
                local_name="call_a2a_agent",
                binding_id=stable_item_id("a2a", space, agent.agent_id),
                space_id=space,
                target_agent_id=agent.agent_id,
                expected_version_id=agent.version_id,
                exposure="tool",
            )
        from ksadk.a2a.tool_outcomes import assert_target_outcome_known, unknown_service_outcome

        await assert_target_outcome_known(self.authority, spec.target_agent_id)
        provider = ADKInvocationContextProvider(
            authority=self.authority,
            service_for_space=self.services,
            caller_agent_id=self.bootstrap.runtime_metadata.agent_id,
            message_selector=lambda *_: (message,),
            workflow_trigger=lambda *_: (trigger, ()),
        )
        resolved = await provider(ctx, spec)
        service, request = resolved.service, resolved.request
        texts = {}
        item_kinds = {}
        try:
            async with aclosing(invoke_with_input(service, request, trigger_ref=trigger)) as events:
                async for event in events:
                    if event.event_type == "item.completed" and event.item_kind in {
                        "message",
                        "artifact",
                    }:
                        item_kinds[(event.scope_id, event.item_id)] = event.item_kind
                        texts[(event.scope_id, event.item_id)] = {
                            part.part_id: part.text
                            for part in event.snapshot.parts
                            if isinstance(part, TextContent)
                        }
                    await self.emit(_project(event, ctx.agent.name, ctx))
        except asyncio.CancelledError:
            try:
                await asyncio.shield(service.cancel(service.handle(request.scope_id)))
            finally:
                raise
        except Exception as exc:
            unknown = unknown_service_outcome(service, request.scope_id)
            if unknown is not None:
                raise unknown from exc
            if isinstance(exc, TimeoutError):
                try:
                    await asyncio.shield(service.cancel(service.handle(request.scope_id)))
                finally:
                    raise
            raise
        snapshot = service.snapshot(service.handle(request.scope_id))
        if unknown := unknown_service_outcome(service, request.scope_id):
            raise unknown
        from ksadk.a2a.result_text import select_terminal_reply

        return {
            "ok": snapshot.status == "completed",
            "agent": spec.target_agent_id,
            "task_id": snapshot.handle.platform_task_id,
            "state": snapshot.status,
            "reply": select_terminal_reply(snapshot.output_refs, texts, item_kinds),
        }


@contextmanager
def bind_managed_adk_tools(runtime):
    token = _binding.set(runtime)
    try:
        yield
    finally:
        _binding.reset(token)
