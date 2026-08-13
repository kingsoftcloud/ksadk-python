"""Native RuntimeAdapter for YAML Harness agents."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ksadk.events import EventPhase, EventType, RuntimeEvent
from ksadk.harness.config import HarnessConfig
from ksadk.harness.reasoner import HarnessReasoner, LiteLLMHarnessReasoner
from ksadk.harness.sandbox import HarnessSandboxExecutor
from ksadk.harness.tools import HarnessTool, load_mcp_tools, sandbox_tools, tool_result_text
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)

_MAX_REASONING_TURNS = 8


class HarnessRuntime(BaseRuntime):
    runtime_type = "harness"

    def native_capabilities(self) -> dict[str, Any]:
        return {
            "cancel": {"supported": True},
            "resume": {"supported": False},
            "checkpoint": {"supported": False, "granularity": "none"},
            "session_continuity": {"durable": False, "scope": "process"},
        }


@dataclass
class _HarnessRun:
    request: StartRequest
    task: asyncio.Task[dict[str, Any]] | None = None
    pending_cancel: bool = False
    done: bool = False


class HarnessRuntimeAdapter(RuntimeAdapter):
    """Execute a YAML Harness config directly as RuntimeEvent streams."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        agent_name: str = "harness-agent",
        reasoner: HarnessReasoner | None = None,
        workspace_root: str | Path = ".",
    ) -> None:
        super().__init__(HarnessRuntime())
        self._config = config
        self._agent_name = agent_name
        self._reasoner = reasoner or LiteLLMHarnessReasoner()
        self._sandbox = HarnessSandboxExecutor(
            workspace_root=workspace_root,
            read_only=config.sandbox.read_only,
        )
        self._runs: dict[str, _HarnessRun] = {}
        self._tools: tuple[HarnessTool, ...] | None = None
        self._tool_lock = asyncio.Lock()
        self._mcp_toolsets: list[Any] = []

    @property
    def harness_config(self) -> HarnessConfig:
        return self._config

    @property
    def sandbox(self) -> HarnessSandboxExecutor:
        return self._sandbox

    @property
    def workspace_root(self) -> Path:
        return self._sandbox.workspace_root

    async def start(self, request: StartRequest) -> RunHandle:
        run_id = str(request.metadata.get("invocation_id") or f"harness_{uuid.uuid4().hex}")
        if run_id in self._runs:
            raise ValueError(f"duplicate Harness invocation: {run_id}")
        self._runs[run_id] = _HarnessRun(request=request)
        return RunHandle(
            run_id=run_id,
            session_id=request.session_id,
            runtime_type="harness",
            native_ref={"user_id": request.user_id, "agent_id": request.agent_id},
        )

    def stream(self, handle: RunHandle):
        return self._stream(handle)

    async def cancel(self, handle: RunHandle) -> CancelResult:
        run = self._runs.get(handle.run_id)
        if run is None or run.done:
            return CancelResult.NOT_RUNNING
        if run.task is None:
            run.pending_cancel = True
            return CancelResult.PENDING_CANCEL_RECORDED
        if run.task.done():
            return CancelResult.NOT_RUNNING
        run.task.cancel()
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        del handle, target, payload
        raise RuntimeError("Harness runtime does not support resume")

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id=f"unsupported:{handle.run_id}",
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=False,
                granularity="none",
                rollback_scope="none",
                fork_supported=False,
                durable=False,
                shared_across_pods=False,
                reason="Harness runtime has no checkpoint backend",
            ),
        )

    async def close(self, handle: RunHandle) -> None:
        run = self._runs.pop(handle.run_id, None)
        if run is not None and run.task is not None and not run.task.done():
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
        if not self._runs:
            await self._close_tools()

    def is_handle_attached(self, handle: RunHandle) -> bool:
        return handle.run_id in self._runs

    async def execute_request(self, request: StartRequest) -> dict[str, Any]:
        tools = await self._ensure_tools()
        model, prompt = self._effective(request)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": str(request.input or "")},
        ]
        execution_log: list[dict[str, Any]] = []

        for _turn_number in range(_MAX_REASONING_TURNS):
            turn = await self._reasoner.complete(
                model=model,
                prompt=prompt,
                messages=tuple(messages),
                tools=tools,
            )
            if turn.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.final_text,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                            for call in turn.tool_calls
                        ],
                    }
                )
                for call in turn.tool_calls:
                    tool = next((item for item in tools if item.name == call.name), None)
                    if tool is None:
                        raise RuntimeError(
                            f"Harness tool {call.name!r} is not available; it may be filtered "
                            "or was not exposed by its MCP server"
                        )
                    result = await tool.call(call.arguments, call_id=call.call_id)
                    content = tool_result_text(result)
                    execution_log.append(
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "source": tool.source,
                            "arguments": dict(call.arguments),
                            "result": result,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "name": call.name,
                            "content": content,
                        }
                    )
                continue
            if turn.final_text is None:
                raise RuntimeError(
                    "Harness reasoner returned neither a final response nor a tool call"
                )
            return {
                "output": turn.final_text,
                "model": model,
                "prompt": prompt,
                "tool_calls": execution_log,
                "sandbox_read_only": self._config.sandbox.read_only,
            }
        raise RuntimeError(f"Harness reasoning exceeded {_MAX_REASONING_TURNS} turns")

    def _effective(self, request: StartRequest) -> tuple[str, str]:
        metadata = request.metadata or {}
        model = str(
            metadata.get("model_override") or request.model or self._config.model
        ).strip()
        prompt = str(
            metadata.get("prompt_override")
            or request.config.get("base_instructions")
            or request.config.get("instructions")
            or request.config.get("prompt")
            or self._config.prompt
        ).strip()
        return model, prompt

    async def _stream(self, handle: RunHandle):
        run = self._require_run(handle)
        seq = 0

        def event(
            event_type: str,
            payload: dict[str, Any],
            *,
            phase: str | None = None,
        ) -> RuntimeEvent:
            nonlocal seq
            seq += 1
            request = run.request
            return RuntimeEvent.create(
                event_type,
                agent_id=str(request.agent_id or self._agent_name),
                user_id=request.user_id,
                session_id=request.session_id,
                invocation_id=handle.run_id,
                seq_id=seq,
                payload=payload,
                phase=phase,
            )

        if run.pending_cancel:
            run.done = True
            yield event(
                EventType.RUN_CANCELED,
                {
                    "status": "cancelled",
                    "cancel_result": CancelResult.PENDING_CANCEL_RECORDED.value,
                },
            )
            return
        yield event(EventType.RUN_STARTED, {"status": "in_progress"})
        run.task = asyncio.create_task(self.execute_request(run.request))
        try:
            result = await run.task
            for call in result["tool_calls"]:
                yield event(
                    EventType.TOOL_CALL_BEGIN,
                    {
                        "call_id": call["call_id"],
                        "name": call["name"],
                        "args": call["arguments"],
                    },
                )
                yield event(
                    EventType.TOOL_CALL_END,
                    {
                        "call_id": call["call_id"],
                        "name": call["name"],
                        "result": call["result"],
                    },
                )
            text = str(result["output"])
            yield event(
                EventType.TEXT_DELTA,
                {"text": text},
                phase=EventPhase.FINAL_ANSWER.value,
            )
            yield event(
                EventType.TEXT_COMPLETED,
                {"text": text},
                phase=EventPhase.FINAL_ANSWER.value,
            )
            run.done = True
            yield event(EventType.RUN_COMPLETED, {"status": "completed"})
        except asyncio.CancelledError:
            run.done = True
            yield event(
                EventType.RUN_CANCELED,
                {
                    "status": "cancelled",
                    "cancel_result": CancelResult.INTERRUPTED_ACTIVE_TURN.value,
                },
            )
        except Exception as exc:  # noqa: BLE001
            run.done = True
            yield event(
                EventType.RUN_FAILED,
                {"status": "failed", "error": str(exc)},
            )

    def _require_run(self, handle: RunHandle) -> _HarnessRun:
        if handle.runtime_type != "harness":
            raise ValueError(f"Harness adapter cannot stream {handle.runtime_type!r}")
        try:
            return self._runs[handle.run_id]
        except KeyError:
            raise KeyError(f"unknown Harness run: {handle.run_id}") from None

    async def _ensure_tools(self) -> tuple[HarnessTool, ...]:
        if self._tools is not None:
            return self._tools
        async with self._tool_lock:
            if self._tools is not None:
                return self._tools
            tools = sandbox_tools(self._sandbox)
            try:
                for spec in self._config.mcp_tools:
                    toolset, discovered = await load_mcp_tools(spec)
                    self._mcp_toolsets.append(toolset)
                    tools.extend(discovered)
                duplicates = sorted(
                    {
                        tool.name
                        for tool in tools
                        if sum(item.name == tool.name for item in tools) > 1
                    }
                )
                if duplicates:
                    raise RuntimeError(f"Harness tool names are not unique: {duplicates}")
            except Exception:
                await self._close_tools()
                raise
            self._tools = tuple(tools)
            return self._tools

    async def _close_tools(self) -> None:
        toolsets, self._mcp_toolsets = self._mcp_toolsets, []
        self._tools = None
        if toolsets:
            await asyncio.gather(*(toolset.close() for toolset in toolsets), return_exceptions=True)


__all__ = ["HarnessRuntime", "HarnessRuntimeAdapter"]
