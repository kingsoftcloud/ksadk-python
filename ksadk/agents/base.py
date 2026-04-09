"""编排 agent 基类。"""

from __future__ import annotations

import inspect
import keyword
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import replace
from typing import Any

from ksadk.agents.context import OrchestrationContext
from ksadk.agents.event import AgentEvent, EventType

logger = logging.getLogger(__name__)

SubAgentType = Any
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")
_TOP_LEVEL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class BaseOrchestrationAgent(ABC):
    """多 agent 编排基类。"""

    def __init__(
        self,
        name: str,
        sub_agents: list[SubAgentType] | None = None,
        description: str = "",
        before_run_hook: Callable[[OrchestrationContext], Any] | None = None,
        after_run_hook: Callable[[OrchestrationContext], Any] | None = None,
    ):
        self._validate_top_level_name(name)
        self.name = name
        self.description = description
        self.sub_agents = sub_agents or []
        self.before_run_hook = before_run_hook
        self.after_run_hook = after_run_hook
        self._validate_sub_agents(self.sub_agents)

    async def run_async(
        self, context: OrchestrationContext | None = None
    ) -> AsyncGenerator[AgentEvent, None]:
        run_context = context or OrchestrationContext()
        logger.info("[%s] orchestration started", self.name)
        yield AgentEvent(
            agent_name=self.name,
            event_type=EventType.AGENT_START,
            branch=run_context.branch,
        )

        try:
            async for hook_event in self._run_hook(self.before_run_hook, run_context):
                yield hook_event

            async for event in self._run_impl(run_context):
                yield self._ensure_event_branch(event, run_context.branch)

            async for hook_event in self._run_hook(self.after_run_hook, run_context):
                yield hook_event
        except Exception as exc:
            yield AgentEvent(
                agent_name=self.name,
                event_type=EventType.ERROR,
                data=str(exc),
                branch=run_context.branch,
                metadata={"exception_type": type(exc).__name__},
            )
            raise

        logger.info("[%s] orchestration finished", self.name)
        yield AgentEvent(
            agent_name=self.name,
            event_type=EventType.AGENT_END,
            branch=run_context.branch,
        )

    @abstractmethod
    async def _run_impl(self, context: OrchestrationContext) -> AsyncGenerator[AgentEvent, None]:
        if False:
            yield AgentEvent(agent_name=self.name, event_type=EventType.TEXT_OUTPUT)

    async def _run_sub_agent(
        self, agent: SubAgentType, context: OrchestrationContext
    ) -> AsyncGenerator[AgentEvent, None]:
        agent_name = self._get_agent_name(agent)

        if isinstance(agent, BaseOrchestrationAgent):
            async for event in agent.run_async(context):
                yield self._apply_event_state(event, context)
            return

        if self._is_runner_like(agent):
            async for event in self._run_runner_like(agent_name, agent, context):
                yield event
            return

        if callable(agent):
            async for event in self._run_callable(agent_name, agent, context):
                yield event
            return

        raise TypeError(f"Unsupported sub-agent type: {type(agent)!r}")

    @classmethod
    def _get_agent_name(cls, agent: SubAgentType) -> str:
        if isinstance(agent, BaseOrchestrationAgent):
            cls._validate_identifier_name(agent.name)
            return agent.name

        explicit_name = getattr(agent, "name", None)
        if isinstance(explicit_name, str) and explicit_name:
            cls._validate_identifier_name(explicit_name)
            return explicit_name

        if inspect.isfunction(agent) or inspect.ismethod(agent):
            inferred_name = getattr(agent, "__name__", "")
            cls._validate_identifier_name(inferred_name)
            return inferred_name

        class_name = agent.__class__.__name__
        inferred_name = _CAMEL_BOUNDARY_RE.sub("_", class_name).lower().lstrip("_")
        cls._validate_identifier_name(inferred_name)
        return inferred_name

    @classmethod
    def _validate_identifier_name(cls, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("Agent name must be a non-empty string")
        if not name.isidentifier() or keyword.iskeyword(name):
            raise ValueError(f"Agent name must be a valid identifier: {name}")

    @classmethod
    def _validate_top_level_name(cls, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("Agent name must be a non-empty string")
        if not _TOP_LEVEL_NAME_RE.match(name):
            raise ValueError(f"Agent name must be a valid identifier: {name}")

    @classmethod
    def _validate_sub_agents(cls, sub_agents: list[SubAgentType]) -> None:
        names: list[str] = []
        for agent in sub_agents:
            if cls._is_runner_like(agent):
                names.append(cls._get_agent_name(agent))
                continue

            if callable(agent):
                names.append(cls._get_agent_name(agent))
                if not (inspect.iscoroutinefunction(agent) or inspect.isasyncgenfunction(agent)):
                    raise TypeError("Custom callable sub-agents must be async")
                continue

            if isinstance(agent, BaseOrchestrationAgent):
                names.append(cls._get_agent_name(agent))
                continue

            raise TypeError(f"Unsupported sub-agent type: {type(agent)!r}")

        if len(names) != len(set(names)):
            raise ValueError(f"Sub-agent names must be unique: {names}")

    @staticmethod
    def _is_runner_like(agent: Any) -> bool:
        invoke = getattr(agent, "invoke", None)
        return callable(invoke)

    async def _run_callable(
        self,
        agent_name: str,
        agent: Callable[[OrchestrationContext], Any],
        context: OrchestrationContext,
    ) -> AsyncGenerator[AgentEvent, None]:
        try:
            result = agent(context)
            if inspect.isasyncgen(result):
                async for item in result:
                    yield self._apply_event_state(
                        self._coerce_event(agent_name, item, context.branch),
                        context,
                    )
                return

            if not inspect.isawaitable(result):
                raise TypeError("Custom callable sub-agents must be async")

            value = await result
        except Exception as exc:
            yield AgentEvent(
                agent_name=agent_name,
                event_type=EventType.ERROR,
                data=str(exc),
                branch=context.branch,
                metadata={"exception_type": type(exc).__name__},
            )
            raise

        if value is None:
            return

        if isinstance(value, (list, tuple)):
            for item in value:
                yield self._apply_event_state(
                    self._coerce_event(agent_name, item, context.branch),
                    context,
                )
            return

        yield self._apply_event_state(
            self._coerce_event(agent_name, value, context.branch),
            context,
        )

    async def _run_runner_like(
        self,
        agent_name: str,
        runner: Any,
        context: OrchestrationContext,
    ) -> AsyncGenerator[AgentEvent, None]:
        output_parts: list[Any] = []
        runner_input = self._build_runner_input(context)

        try:
            stream = getattr(runner, "stream", None)
            if callable(stream):
                async for chunk in stream(runner_input):
                    event = self._apply_event_state(
                        self._coerce_event(agent_name, chunk, context.branch),
                        context,
                    )
                    if event.event_type == EventType.TEXT_OUTPUT:
                        output_parts.append(event.data)
                    yield event
            else:
                result = await runner.invoke(runner_input)
                event = self._apply_event_state(
                    self._coerce_event(agent_name, result, context.branch),
                    context,
                )
                if event.event_type == EventType.TEXT_OUTPUT:
                    output_parts.append(event.data)
                yield event
        except Exception as exc:
            yield AgentEvent(
                agent_name=agent_name,
                event_type=EventType.ERROR,
                data=str(exc),
                branch=context.branch,
                metadata={"exception_type": type(exc).__name__},
            )
            raise

        if output_parts:
            context.set(f"{agent_name}_output", self._merge_output_parts(output_parts))

    async def _run_hook(
        self,
        hook: Callable[[OrchestrationContext], Any] | None,
        context: OrchestrationContext,
    ) -> AsyncIterator[AgentEvent]:
        if hook is None:
            return

        result = hook(context)
        if inspect.isawaitable(result):
            result = await result

        if result is None:
            return

        if isinstance(result, (list, tuple)):
            for item in result:
                yield self._coerce_event(self.name, item, context.branch)
            return

        yield self._coerce_event(self.name, result, context.branch)

    @staticmethod
    def _build_runner_input(context: OrchestrationContext) -> dict[str, Any]:
        return {
            "input": context.get("input", ""),
            "state": dict(context.state),
            "session_id": context.session_id,
            "branch": context.branch,
        }

    @staticmethod
    def _merge_output_parts(parts: list[Any]) -> Any:
        if all(isinstance(part, str) for part in parts):
            return "".join(parts)
        if len(parts) == 1:
            return parts[0]
        return parts

    @classmethod
    def _coerce_event(
        cls,
        agent_name: str,
        payload: Any,
        branch: str,
    ) -> AgentEvent:
        if isinstance(payload, AgentEvent):
            event = payload
            if not event.agent_name:
                event = replace(event, agent_name=agent_name)
            if not event.branch:
                event = replace(event, branch=branch)
            return event

        if isinstance(payload, dict):
            raw_event_type = payload.get("event_type")
            event_type = cls._coerce_event_type(raw_event_type)
            data = payload.get("data")
            if data is None:
                data = payload.get("output", payload.get("delta"))
            state_delta = dict(payload.get("state_delta", {}))
            metadata = dict(payload.get("metadata", {}))

            if "type" in payload and "chunk_type" not in metadata:
                metadata["chunk_type"] = payload["type"]

            if event_type is None:
                event_type = (
                    EventType.STATE_CHANGE
                    if state_delta and data is None
                    else EventType.TEXT_OUTPUT
                )

            if data is None and event_type == EventType.STATE_CHANGE and state_delta:
                data = state_delta

            escalate = bool(payload.get("escalate", False) or event_type == EventType.ESCALATE)
            return AgentEvent(
                agent_name=agent_name,
                event_type=event_type,
                data=data,
                branch=branch,
                metadata=metadata,
                state_delta=state_delta,
                escalate=escalate,
            )

        return AgentEvent(
            agent_name=agent_name,
            event_type=EventType.TEXT_OUTPUT,
            data=payload,
            branch=branch,
        )

    @staticmethod
    def _coerce_event_type(raw_event_type: Any) -> EventType | None:
        if raw_event_type is None:
            return None
        if isinstance(raw_event_type, EventType):
            return raw_event_type
        return EventType(str(raw_event_type))

    @staticmethod
    def _ensure_event_branch(event: AgentEvent, branch: str) -> AgentEvent:
        if event.branch or not branch:
            return event
        return replace(event, branch=branch)

    @staticmethod
    def _apply_event_state(event: AgentEvent, context: OrchestrationContext) -> AgentEvent:
        if event.state_delta:
            context.update(event.state_delta)
        return event
