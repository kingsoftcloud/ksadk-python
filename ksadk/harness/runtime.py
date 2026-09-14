"""Native RuntimeAdapter for YAML Harness agents."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ksadk.events.canonical import (
    ErrorInfo,
    ItemCompleted,
    ItemStarted,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunStarted,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import ContentSnapshot, TextContent, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
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


@dataclass
class _HarnessSession:
    """Process-local transcript and serialization boundary for one Session.

    The public capability matrix deliberately advertises process-scoped,
    non-durable continuity.  Keeping this state on the adapter makes that
    declaration true without pretending that a restart can recover it.
    """

    messages: list[dict[str, Any]]
    lock: asyncio.Lock


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
        self._sessions: dict[tuple[str, str, str], _HarnessSession] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

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
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Harness runtime adapter is closed")
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
        async with self._lifecycle_lock:
            run = self._runs.pop(handle.run_id, None)
            has_runs = bool(self._runs)
        if run is not None and run.task is not None and not run.task.done():
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
        if not has_runs:
            await self._close_tools()

    async def close_all(self) -> None:
        """Dispose every process-local run owned by this adapter instance."""

        async with self._lifecycle_lock:
            self._closed = True
            runs = tuple(self._runs.values())
            self._runs.clear()
            self._sessions.clear()
        tasks = [run.task for run in runs if run.task is not None and not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_tools()

    def is_handle_attached(self, handle: RunHandle) -> bool:
        return handle.run_id in self._runs

    async def execute_request(self, request: StartRequest) -> dict[str, Any]:
        session = self._session_for(request)
        async with session.lock:
            return await self._execute_session_request(request, session)

    async def _execute_session_request(
        self,
        request: StartRequest,
        session: _HarnessSession,
    ) -> dict[str, Any]:
        tools = await self._ensure_tools()
        model, prompt = self._effective(request)
        conversation = request.conversation_preprocessing()
        history = (
            [dict(item) for item in conversation.messages]
            if conversation is not None and conversation.messages
            else [dict(item) for item in session.messages]
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        messages.extend(history)
        current_input = str(request.input or "")
        if not history or not (
            str(history[-1].get("role") or "") == "user"
            and str(history[-1].get("content") or "") == current_input
        ):
            messages.append({"role": "user", "content": current_input})
        execution_log: list[dict[str, Any]] = []
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }
        reasoning_parts: list[str] = []

        for _turn_number in range(_MAX_REASONING_TURNS):
            turn = await self._reasoner.complete(
                model=model,
                prompt=prompt,
                messages=tuple(messages),
                tools=tools,
            )
            for key in usage:
                usage[key] += max(0, int((turn.usage or {}).get(key, 0)))
            if turn.reasoning:
                reasoning_parts.append(turn.reasoning)
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
            final_message = {"role": "assistant", "content": turn.final_text}
            messages.append(final_message)
            # Failed or cancelled turns never commit a partial transcript.
            # A successful turn atomically replaces the process-local history
            # while the per-session lock is still held.
            session.messages[:] = [dict(message) for message in messages[1:]]
            return {
                "output": turn.final_text,
                "model": model,
                "prompt": prompt,
                "tool_calls": execution_log,
                "sandbox_read_only": self._config.sandbox.read_only,
                "usage": {
                    **usage,
                    "total_tokens": usage["input_tokens"] + usage["output_tokens"],
                },
                "reasoning": "".join(reasoning_parts),
            }
        raise RuntimeError(f"Harness reasoning exceeded {_MAX_REASONING_TURNS} turns")

    def _session_for(self, request: StartRequest) -> _HarnessSession:
        key = (
            str(request.agent_id or self._agent_name),
            str(request.user_id),
            str(request.session_id),
        )
        session = self._sessions.get(key)
        if session is None:
            session = _HarnessSession(messages=[], lock=asyncio.Lock())
            self._sessions[key] = session
        return session

    def _effective(self, request: StartRequest) -> tuple[str, str]:
        metadata = request.metadata or {}
        model = str(metadata.get("model_override") or request.model or self._config.model).strip()
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

        framework = "ksadk"
        scope_id = stable_scope_id(framework, handle.run_id)
        run_item_id = stable_item_id(framework, handle.run_id, "$run")
        request = run.request
        source = SourceRef(
            framework=framework,
            native_run_id=handle.run_id,
            metadata={
                "agent_id": str(request.agent_id or self._agent_name),
                "user_id": request.user_id,
                "session_id": request.session_id,
                "invocation_id": handle.run_id,
            },
        )

        def envelope(event_type: str, item_id: str, part_id: str) -> dict[str, Any]:
            nonlocal seq
            seq += 1
            return {
                "schema_version": 2,
                "event_id": stable_event_id(
                    framework,
                    scope_id,
                    item_id,
                    event_type,
                    part_id,
                    handle.run_id,
                    seq,
                ),
                "seq": seq,
                "timestamp": time.time(),
                "run_id": handle.run_id,
                "scope_id": scope_id,
                "source": source,
            }

        if run.pending_cancel:
            run.done = True
            yield RunCanceled(
                **envelope("run.canceled", run_item_id, "run"),
                status="canceled",
                reason=CancelResult.PENDING_CANCEL_RECORDED.value,
            )
            return
        yield RunStarted(
            **envelope("run.started", run_item_id, "run"),
            status="running",
        )
        run.task = asyncio.create_task(self.execute_request(run.request))
        try:
            result = await run.task
            usage = result["usage"]
            reasoning_text = str(result.get("reasoning") or "")
            if reasoning_text:
                reasoning_item_id = stable_item_id(
                    framework, handle.run_id, "reasoning", "0"
                )
                reasoning_part_id = "reasoning-0"
                reasoning_content = ContentSnapshot(
                    parts=(TextContent(part_id=reasoning_part_id, text=reasoning_text),)
                )
                yield ItemStarted(
                    **envelope("item.started", reasoning_item_id, reasoning_part_id),
                    item_id=reasoning_item_id,
                    item_kind="reasoning",
                    phase="commentary",
                    initial=reasoning_content,
                )
                yield ItemCompleted(
                    **envelope("item.completed", reasoning_item_id, reasoning_part_id),
                    item_id=reasoning_item_id,
                    item_kind="reasoning",
                    snapshot=reasoning_content,
                )
            yield UsageReported(
                **envelope("usage.reported", run_item_id, "usage"),
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["total_tokens"],
                cached_tokens=usage["cached_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
            )
            for call in result["tool_calls"]:
                call_item_id = stable_item_id(
                    framework, handle.run_id, "tool_call", call["call_id"]
                )
                call_part_id = "tool-call-0"
                call_content = ToolCallContent(
                    part_id=call_part_id,
                    call_id=call["call_id"],
                    name=call["name"],
                    arguments=call["arguments"],
                )
                yield ItemStarted(
                    **envelope("item.started", call_item_id, call_part_id),
                    item_id=call_item_id,
                    item_kind="tool_call",
                    phase="commentary",
                    initial=ContentSnapshot(parts=(call_content,)),
                )
                yield ItemCompleted(
                    **envelope("item.completed", call_item_id, call_part_id),
                    item_id=call_item_id,
                    item_kind="tool_call",
                    snapshot=ContentSnapshot(parts=(call_content,)),
                )
                result_item_id = stable_item_id(
                    framework, handle.run_id, "tool_result", call["call_id"]
                )
                result_part_id = "tool-result-0"
                result_content = ToolResultContent(
                    part_id=result_part_id,
                    call_id=call["call_id"],
                    result=call["result"],
                )
                yield ItemStarted(
                    **envelope("item.started", result_item_id, result_part_id),
                    item_id=result_item_id,
                    item_kind="tool_result",
                    phase="commentary",
                    initial=ContentSnapshot(parts=(result_content,)),
                )
                yield ItemCompleted(
                    **envelope("item.completed", result_item_id, result_part_id),
                    item_id=result_item_id,
                    item_kind="tool_result",
                    snapshot=ContentSnapshot(parts=(result_content,)),
                )
            text = str(result["output"])
            message_item_id = stable_item_id(framework, handle.run_id, "message", "final_answer")
            text_part_id = "text-0"
            yield ItemStarted(
                **envelope("item.started", message_item_id, text_part_id),
                item_id=message_item_id,
                item_kind="message",
                phase="final_answer",
                initial=None,
            )
            yield ItemCompleted(
                **envelope("item.completed", message_item_id, text_part_id),
                item_id=message_item_id,
                item_kind="message",
                snapshot=ContentSnapshot(parts=(TextContent(part_id=text_part_id, text=text),)),
            )
            run.done = True
            yield RunCompleted(
                **envelope("run.completed", run_item_id, "run"),
                status="completed",
                output_refs=(
                    OutputRef(
                        scope_id=scope_id,
                        item_id=message_item_id,
                        part_id=text_part_id,
                    ),
                ),
            )
        except asyncio.CancelledError:
            run.done = True
            yield RunCanceled(
                **envelope("run.canceled", run_item_id, "run"),
                status="canceled",
                reason=CancelResult.INTERRUPTED_ACTIVE_TURN.value,
            )
        except Exception as exc:  # noqa: BLE001
            run.done = True
            yield RunFailed(
                **envelope("run.failed", run_item_id, "run"),
                status="failed",
                error=ErrorInfo(
                    code="HARNESS_EXECUTION_FAILED",
                    message=str(exc),
                    source="ksadk.harness",
                    scope_id=scope_id,
                ),
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
