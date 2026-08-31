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
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunStarted,
    SourceRef,
)
from ksadk.events.content import (
    ContentSnapshot,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.identity import (
    stable_event_id,
    stable_item_id,
    stable_scope_id,
)
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

    async def close_all(self) -> None:
        """Dispose every process-local run owned by this adapter instance."""

        for run_id, run in list(self._runs.items()):
            await self.close(
                RunHandle(
                    run_id=run_id,
                    session_id=run.request.session_id,
                    runtime_type="harness",
                    native_ref={
                        "user_id": run.request.user_id,
                        "agent_id": run.request.agent_id,
                    },
                )
            )

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
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            *[dict(message) for message in session.messages],
        ]
        user_message = {"role": "user", "content": str(request.input or "")}
        messages.append(user_message)
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
        framework = "ksadk"
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        message_item_id = stable_item_id(framework, run_id, "message", "final_answer")
        run_item_id = stable_item_id(framework, run_id, "$run")
        seq = 0
        started_items: set[tuple[str, str]] = set()

        def next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        def make_source() -> SourceRef:
            return SourceRef(
                framework=framework,
                native_run_id=run_id,
                metadata={
                    "agent_id": str(run.request.agent_id or self._agent_name),
                    "user_id": run.request.user_id,
                    "session_id": run.request.session_id,
                    "invocation_id": run_id,
                },
            )

        def env_kwargs(
            item_id: str, event_type: str, part_id: str
        ) -> dict[str, Any]:
            n = next_seq()
            return {
                "schema_version": 2,
                "event_id": stable_event_id(
                    framework, scope_id, item_id, event_type, part_id, run_id, n
                ),
                "seq": n,
                "timestamp": time.time(),
                "run_id": run_id,
                "scope_id": scope_id,
                "source": make_source(),
            }

        def ensure_started(
            item_id: str,
            item_kind: str,
            phase: str | None = None,
            initial: ContentSnapshot | None = None,
        ) -> list[ItemStarted]:
            key = (scope_id, item_id)
            if key in started_items:
                return []
            started_items.add(key)
            return [
                ItemStarted(
                    **env_kwargs(item_id, "item.started", "item"),
                    item_id=item_id,
                    item_kind=item_kind,
                    phase=phase,
                    initial=initial,
                )
            ]

        if run.pending_cancel:
            run.done = True
            yield RunCanceled(
                **env_kwargs(run_item_id, "run.canceled", "run"),
                status="canceled",
                reason=CancelResult.PENDING_CANCEL_RECORDED.value,
            )
            return
        yield RunStarted(
            **env_kwargs(run_item_id, "run.started", "run"),
            status="running",
        )
        run.task = asyncio.create_task(self.execute_request(run.request))
        try:
            result = await run.task
            for call in result["tool_calls"]:
                call_id = call["call_id"]
                tool_item_id = stable_item_id(framework, run_id, "tool_call", call_id)
                for ev in ensure_started(
                    item_id=tool_item_id,
                    item_kind="tool_call",
                    initial=ContentSnapshot(
                        parts=(
                            ToolCallContent(
                                part_id="tool-0",
                                call_id=call_id,
                                name=call["name"],
                                arguments=call["arguments"],
                            ),
                        )
                    ),
                ):
                    yield ev
                yield ItemCompleted(
                    **env_kwargs(tool_item_id, "item.completed", "tool-0"),
                    item_id=tool_item_id,
                    item_kind="tool_call",
                    snapshot=ContentSnapshot(
                        parts=(
                            ToolResultContent(
                                part_id="tool-0",
                                call_id=call_id,
                                result=call["result"],
                            ),
                        )
                    ),
                )
            text = str(result["output"])
            for ev in ensure_started(
                item_id=message_item_id,
                item_kind="message",
                phase="final_answer",
            ):
                yield ev
            yield ItemUpdated(
                **env_kwargs(message_item_id, "item.updated", "text-0"),
                item_id=message_item_id,
                item_kind="message",
                op="append",
                update=TextContent(part_id="text-0", text=text),
            )
            yield ItemCompleted(
                **env_kwargs(message_item_id, "item.completed", "text-0"),
                item_id=message_item_id,
                item_kind="message",
                snapshot=ContentSnapshot(
                    parts=(TextContent(part_id="text-0", text=text),)
                ),
            )
            run.done = True
            yield RunCompleted(
                **env_kwargs(run_item_id, "run.completed", "run"),
                status="completed",
                output_refs=(
                    OutputRef(
                        scope_id=scope_id,
                        item_id=message_item_id,
                        part_id="text-0",
                    ),
                ),
            )
        except asyncio.CancelledError:
            run.done = True
            yield RunCanceled(
                **env_kwargs(run_item_id, "run.canceled", "run"),
                status="canceled",
                reason=CancelResult.INTERRUPTED_ACTIVE_TURN.value,
            )
        except Exception as exc:  # noqa: BLE001
            run.done = True
            yield RunFailed(
                **env_kwargs(run_item_id, "run.failed", "run"),
                status="failed",
                error=ErrorInfo(
                    code="harness_failed",
                    message=str(exc),
                    source=framework,
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
