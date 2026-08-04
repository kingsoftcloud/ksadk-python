"""Studio Run persistence on top of the canonical RuntimeExecutor."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    RuntimeExecutor,
    RuntimeLaunchContext,
    StartRequest,
)
from ksadk.studio.contracts import RunEvent, RunRecord, RunStatus, Usage
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.workspace import Workspace


@dataclass(frozen=True)
class StudioRunSpec:
    """Product metadata plus the core launch context for one Studio run."""

    launch_context: RuntimeLaunchContext
    build_id: str
    agent_id: str
    model: str | None = None
    request_config: Mapping[str, Any] = field(default_factory=dict)
    manifest_sha256: str = ""


class StudioRunService:
    """Persist Studio state without selecting or wrapping a RuntimeAdapter."""

    def __init__(
        self,
        workspace: Workspace,
        executor: RuntimeExecutor,
        *,
        event_store: RunEventStore | None = None,
    ) -> None:
        self.workspace = workspace
        self.executor = executor
        self.event_store = event_store or RunEventStore(workspace)

    async def run(
        self,
        spec: StudioRunSpec,
        user_input: str,
        *,
        session_id: str | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> RunRecord:
        run_id = f"run_{uuid4().hex}"
        session = session_id or f"ses_{uuid4().hex}"
        runtime_type = spec.launch_context.runtime_type.strip().lower()
        record = RunRecord(
            id=run_id,
            build_id=spec.build_id,
            agent_id=spec.agent_id,
            session_id=session,
            trace_id=uuid4().hex,
            manifest_sha256=spec.manifest_sha256,
            runtime_type=runtime_type,
            model=str(spec.model or ""),
            input=user_input,
        )
        self.event_store.create(record)
        created = self.event_store.append(
            record.id,
            "run.created",
            {
                "buildId": spec.build_id,
                "sessionId": session,
                "traceId": record.trace_id,
                "runtimeType": runtime_type,
                "manifestSha256": spec.manifest_sha256,
                "model": record.model,
            },
        )
        if on_event is not None:
            on_event(created)

        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        self.event_store.save(record)

        def persist(runtime_event: RuntimeEvent) -> RunEvent:
            event_type, data = project_runtime_event(runtime_event)
            stored = self.event_store.append(record.id, event_type, data)
            if on_event is not None:
                on_event(stored)
            return stored

        handle = None
        final_text = ""
        streamed_final = ""
        runtime_duration_ms: int | None = None
        try:
            request = StartRequest(
                input=user_input,
                user_id="local-user",
                session_id=session,
                agent_id=spec.agent_id,
                model=spec.model,
                config=dict(spec.request_config),
                metadata={
                    "invocation_id": run_id,
                    CONVERSATION_PREPROCESSING_METADATA_KEY: {
                        "messages": self._conversation_messages(
                            spec.agent_id,
                            session,
                            user_input,
                        )
                    },
                },
            )
            handle = await self.executor.start(spec.launch_context, request)
            record.runtime_handle = handle.model_dump(mode="json")
            self.event_store.save(record)
            terminal_seen = False
            async for event in self.executor.stream(handle):
                persist(event)
                if event.event_type == EventType.TEXT_DELTA and event.phase == "final_answer":
                    text = str(event.payload.get("text") or "")
                    if event.payload.get("replace"):
                        streamed_final = text
                    else:
                        streamed_final += text
                elif (
                    event.event_type == EventType.TEXT_COMPLETED
                    and event.phase == "final_answer"
                ):
                    final_text = str(event.payload.get("text") or "")
                elif event.event_type == EventType.USAGE_REPORTED:
                    record.usage = Usage(
                        input_tokens=int(event.payload.get("input_tokens") or 0),
                        output_tokens=int(event.payload.get("output_tokens") or 0),
                        total_tokens=int(event.payload.get("total_tokens") or 0),
                        cached_input_tokens=int(event.payload.get("cached_tokens") or 0),
                        reasoning_output_tokens=int(
                            event.payload.get("reasoning_tokens") or 0
                        ),
                        reported=True,
                        source=str(event.payload.get("source") or runtime_type),
                    )
                elif event.event_type == EventType.RUN_FAILED:
                    terminal_seen = True
                    record.status = RunStatus.FAILED
                    record.error = {
                        "code": "RUNTIME_RUN_FAILED",
                        "message": str(event.payload.get("error") or "Runtime 运行失败"),
                    }
                elif event.event_type in {
                    EventType.RUN_CANCELED,
                    EventType.RUN_INTERRUPTED,
                }:
                    terminal_seen = True
                    record.status = (
                        RunStatus.INTERRUPTED
                        if event.event_type == EventType.RUN_INTERRUPTED
                        else RunStatus.CANCELLED
                    )
                    code = (
                        "RUN_INTERRUPTED"
                        if event.event_type == EventType.RUN_INTERRUPTED
                        else "RUN_CANCELLED"
                    )
                    record.error = {
                        "code": code,
                        "message": str(event.payload.get("status") or "运行已取消"),
                    }
                elif event.event_type == EventType.RUN_COMPLETED:
                    terminal_seen = True
                    record.status = RunStatus.COMPLETED
                    raw_duration = event.payload.get("duration_ms")
                    if raw_duration is not None:
                        runtime_duration_ms = max(0, int(raw_duration))
            if not terminal_seen:
                record.status = RunStatus.COMPLETED
            record.output = final_text or streamed_final
        except asyncio.CancelledError:
            cancel_result = "task_cancelled"
            if handle is not None and self.executor.is_attached(handle):
                try:
                    result = await asyncio.shield(self.executor.cancel(handle))
                    cancel_result = result.value
                except Exception:  # best effort; close still owns resource cleanup
                    cancel_result = "cancel_failed"
            record.status = RunStatus.CANCELLED
            record.error = {"code": "RUN_CANCELLED", "message": "运行已取消"}
            cancelled = RuntimeEvent.create(
                EventType.RUN_CANCELED,
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=session,
                invocation_id=handle.run_id if handle is not None else run_id,
                seq_id=len(self.event_store.events(run_id)) + 1,
                payload={"status": "cancelled", "cancel_result": cancel_result},
            )
            persist(cancelled)
            raise
        except Exception as exc:  # noqa: BLE001
            record.status = RunStatus.FAILED
            record.error = {"code": "RUNTIME_RUN_FAILED", "message": str(exc)}
            failure = RuntimeEvent.create(
                EventType.RUN_FAILED,
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=session,
                invocation_id=handle.run_id if handle is not None else run_id,
                seq_id=len(self.event_store.events(run_id)) + 1,
                payload={"status": "failed", "error": str(exc)},
            )
            persist(failure)
        finally:
            if handle is not None and self.executor.is_attached(handle):
                try:
                    await asyncio.shield(self.executor.close(handle))
                except asyncio.CancelledError:
                    pass
                except Exception:  # cleanup is best effort; run state is already durable
                    pass
            record.completed_at = datetime.now(timezone.utc)
            if runtime_duration_ms is not None:
                record.duration_ms = runtime_duration_ms
                record.duration_source = "runtime"
            else:
                record.duration_ms = int((time.monotonic() - started) * 1000)
                record.duration_source = "studio"
            self.event_store.save(record)
        return record

    def _conversation_messages(
        self,
        agent_id: str,
        session_id: str,
        user_input: str,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for previous in self.event_store.list_runs(session_id=session_id):
            if previous.agent_id != agent_id or previous.status != RunStatus.COMPLETED:
                continue
            messages.append({"role": "user", "content": previous.input})
            messages.append({"role": "assistant", "content": previous.output})
        messages.append({"role": "user", "content": user_input})
        return messages


def project_runtime_event(event: RuntimeEvent) -> tuple[str, dict[str, Any]]:
    """Project the canonical RuntimeEvent into Studio's persisted event view."""

    payload = dict(event.payload)
    event_type = event.event_type
    if event_type == EventType.RUN_STARTED:
        projected = "run.started"
    elif event_type == EventType.RUN_PROGRESS:
        projected = str(payload.get("native_event") or "run.progress")
        native_data = payload.get("native_data")
        if isinstance(native_data, dict):
            payload = dict(native_data)
    elif event_type in {EventType.REASONING_DELTA, EventType.REASONING_COMPLETED}:
        projected = (
            "thinking.delta"
            if event_type == EventType.REASONING_DELTA
            else "thinking.completed"
        )
    elif event_type == EventType.TEXT_DELTA:
        projected = "message.delta" if event.phase == "final_answer" else "thinking.delta"
    elif event_type == EventType.TEXT_COMPLETED:
        projected = (
            "message.completed" if event.phase == "final_answer" else "thinking.completed"
        )
    elif event_type == EventType.TOOL_CALL_BEGIN:
        projected = (
            "command.started" if payload.get("name") == "codex.command" else "tool.started"
        )
        if projected == "command.started":
            raw_args = payload.get("args")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            payload = {
                "callId": str(payload.get("call_id") or ""),
                "command": str(args.get("command") or ""),
                "cwd": str(args.get("cwd") or ""),
                "commandActions": args.get("command_actions") or [],
            }
    elif event_type == EventType.TOOL_CALL_END:
        projected = (
            "command.completed"
            if payload.get("name") == "codex.command"
            else "tool.completed"
        )
        if projected == "command.completed":
            raw_result = payload.get("result")
            result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
            payload = {
                "callId": str(payload.get("call_id") or ""),
                "status": str(result.get("status") or ""),
                "exitCode": result.get("exit_code"),
                "durationMs": result.get("duration_ms"),
                "output": str(result.get("output") or ""),
            }
    elif event_type == EventType.RUN_COMPLETED:
        projected = "run.completed"
    elif event_type == EventType.RUN_FAILED:
        projected = "run.failed"
    elif event_type == EventType.RUN_CANCELED:
        projected = "run.cancelled"
    elif event_type == EventType.RUN_INTERRUPTED:
        projected = "run.interrupted"
    else:
        projected = str(event_type)
    payload["runtimeEvent"] = event.to_dict()
    return projected, payload


__all__ = ["StudioRunService", "StudioRunSpec", "project_runtime_event"]
