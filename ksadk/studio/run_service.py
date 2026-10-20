"""Studio Run persistence on top of the canonical RuntimeExecutor."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import linecache
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.conversations.projector import (
    project_conversation_item,
    project_interaction_conversation_item,
)
from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemFailed,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    UsageReported,
    dump_runtime_event,
    parse_runtime_event,
)
from ksadk.events.canonical_store import session_event_to_runtime_event
from ksadk.events.content import (
    ContentSnapshot,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.store import RuntimeEventStore
from ksadk.runtime import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    ResumePayload,
    ResumeTarget,
    RuntimeExecutor,
    RuntimeLaunchContext,
    StartRequest,
)
from ksadk.runtime.executor import RuntimeStartPreparation
from ksadk.sessions.base import BaseSessionService
from ksadk.sessions.local_service import LocalSessionService
from ksadk.studio.contracts import RunEvent, RunRecord, RunStatus, RunSubmissionRef, Usage
from ksadk.studio.errors import StudioError
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.native_background_wakeup import NativeBackgroundWakeup
from ksadk.studio.native_verification_presentation import terminal_verification_notice
from ksadk.studio.run_activity import project_run_activities
from ksadk.studio.run_controls import StudioRunControlsMixin
from ksadk.studio.run_event_writer import (
    _ProjectedRunEventWriter as _ProjectedRunEventWriter,
    _same_projected_stream as _same_projected_stream,
    _set_projected_stream_text as _set_projected_stream_text,
)
from ksadk.studio.run_wake_dispatcher import (
    _RunServiceWakeDispatcher as _RunServiceWakeDispatcher,
)
from ksadk.studio.workspace import Workspace

_CANCEL_TIMEOUT_SECONDS = 2.0
_INTERACTION_SUBMIT_TIMEOUT_SECONDS = 10.0


logger = logging.getLogger(__name__)


def _stall_seconds(name: str, default: float) -> float:
    """读看门狗阈值（秒）；非法值回落到默认，<=0 表示关闭。"""

    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _format_task_stacks(limit: int = 40) -> str:
    """把当前 asyncio 任务现场拼成可读文本（只用于诊断日志，不含凭据）。

    比普通 ``task.get_stack()`` 更进一步——后者只返回**当前协程**那一帧，
    ``await`` 链上被等待协程的帧会整段丢掉（上一版 dump 里 run 本体只剩一帧
    ``ainvoke`` 就是这个原因）。这里改为：

    - 记录 ``task._fut_waiter``：任务到底在等哪个 future / event / task；
    - 沿 ``coro.cr_await`` 逐层取帧，直到撞到 Future/Task/Event 边界。
    """

    parts: list[str] = []
    for task in sorted(asyncio.all_tasks(), key=lambda t: (t.done(), t.get_name())):
        if task.done():
            continue
        coro = task.get_coro()
        parts.append(f"=== task {task.get_name()} ({_coro_qualname(coro)})")
        fw = getattr(task, "_fut_waiter", None)
        if fw is not None:
            parts.append(f"    等待对象: {_future_desc(fw)}")
        frames = _coro_await_frames(coro, limit=limit)
        if not frames:
            parts.append("    (无栈帧)")
            continue
        for filename, lineno, code in frames:
            parts.append(f"    {filename}:{lineno}  {code}")
        parts.append("")
    return "\n".join(parts) or "(没有可 dump 的任务栈)"


def _coro_qualname(coro: Any) -> str:
    try:
        return coro.__qualname__
    except Exception:  # noqa: BLE001 - 诊断尽力而为
        return repr(coro)[:80]


def _future_desc(fut: Any) -> str:
    """描述被 await 的 future/event/task；只读属性，不触发、不等待它。"""

    if isinstance(fut, asyncio.Task):
        state = "done" if fut.done() else "pending"
        detail = ""
        if fut.done() and not fut.cancelled():
            exc = fut.exception()
            detail = f" exc={type(exc).__name__}" if exc is not None else ""
        return f"Task({_coro_qualname(fut.get_coro())}) {state}{detail}"
    if isinstance(fut, asyncio.Event):
        return f"Event is_set={fut.is_set()}"
    if isinstance(fut, asyncio.Future):
        state = "done" if fut.done() else "pending"
        if fut.done() and not fut.cancelled():
            exc = fut.exception()
            state += f" exc={type(exc).__name__}" if exc is not None else ""
        return f"Future {state} cancelled={fut.cancelled()}"
    return f"{type(fut).__name__}: {repr(fut)[:140]}"


def _coro_await_frames(
    coro: Any, *, limit: int
) -> list[tuple[str, int, str]]:
    """沿 ``cr_await`` 链收集 (文件名, 行号, 代码行)；撞到 Future/Task 即停。"""

    frames: list[tuple[str, int, str]] = []
    seen = 0
    guard = 0
    while coro is not None and seen < limit and guard < 256:
        guard += 1
        frame = getattr(coro, "cr_frame", None)
        if frame is not None:
            code = frame.f_code
            line = linecache.getline(code.co_filename, frame.f_lineno).strip()
            frames.append((code.co_filename, frame.f_lineno, line))
            seen += 1
        nxt = getattr(coro, "cr_await", None)
        if nxt is None or nxt is coro:
            break
        if not hasattr(nxt, "cr_frame"):
            # await 的是 Future/Task/Event：链到此为止（fut_waiter 已单独记录）。
            frames.append(("<await-boundary>", 0, _future_desc(nxt)))
            break
        coro = nxt
    return frames


def _format_loop_timers(limit: int = 12) -> str:
    """列出事件循环里最近将触发的定时器（含 callback 与到期秒数）。

    用途：区分"卡在一个超长 sleep"和"在等一个永远不会就绪的 future"。
    前者会在这里看到一个很大的到期秒数；后者这里要么空、要么全是无关的
    轮询定时器，而任务却停在 await 一个 Future 上。
    """

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return "(没有运行中的事件循环)"
    handles = []
    for handle in list(getattr(loop, "_scheduled", ()) or ()):
        if getattr(handle, "_cancelled", False):
            continue
        when = getattr(handle, "_when", None)
        if when is None:
            continue
        callback = getattr(handle, "_callback", None)
        name = getattr(callback, "__qualname__", None) or repr(callback)[:60]
        handles.append((float(when), name))
    if not handles:
        return "(事件循环里没有待触发的定时器)"
    handles.sort()
    try:
        now = loop.time()
    except Exception:  # noqa: BLE001 - 诊断尽力而为
        now = handles[0][0]
    lines = [
        f"    {max(0.0, when - now):9.2f}s 后  {name}"
        for when, name in handles[:limit]
    ]
    if len(handles) > limit:
        lines.append(f"    ... 另有 {len(handles) - limit} 个定时器")
    return "\n".join(lines)


def _consumed_model_output(record: Any) -> bool:
    """该 run 是否真的消耗了模型输出 token（用于判定"空交付"而非"合法空输出"）。

    审批/交互类流程会合法地以空输出收束；只有"跑过模型、烧了输出 token，却没有任何
    交付内容"才是事故（2026-09-18 实测：harness provider adapter 路径 COMPLETED +
    output 长度 0 + 产物 0）。
    """

    usage = getattr(record, "usage", None)
    try:
        return int(getattr(usage, "output_tokens", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _require_delivery(output: Any) -> None:
    """空交付不得判 COMPLETED（2026-09-18 实测事故）。

    一次 run 跑了 15 次模型调用、并行 3 个子 Agent、约 17 分钟，最后以
    **COMPLETED + output 长度 0 + 产物 0** 收场——用户看到"成功"却拿不到任何东西。
    harness 的真正完成点是 ``_plugin_run`` / ``_kernel_run``（我第一版把守卫错放在
    ``_schedule_run``，于是下一次事故照样以 COMPLETED 收场），两处都必须过这道闸。
    """

    if not str(output or "").strip():
        raise StudioError(
            "EMPTY_DELIVERY",
            "本轮没有产出任何交付内容（输出为空，且没有产物），已按失败处理而不是成功。"
            "请重试，或检查该 Agent 的委派/落盘策略。",
            status_code=502,
        )


@dataclass(frozen=True)
class StudioRunSpec:
    """Product metadata plus the core launch context for one Studio run."""

    launch_context: RuntimeLaunchContext
    build_id: str
    agent_id: str
    model: str | None = None
    # Immutable capability metadata copied from the selected model in the
    # Build.  Conversation surfaces must use this declaration instead of
    # guessing modality support from model names or mutable catalog state.
    model_metadata: Mapping[str, Any] = field(default_factory=dict)
    request_config: Mapping[str, Any] = field(default_factory=dict)
    manifest_sha256: str = ""
    plugin_bundle_root: Path | None = None
    submission_ref: RunSubmissionRef | None = None
    presentation_profile: Literal["flat-v1", "agent-block-v1"] = "flat-v1"








class StudioRunService(StudioRunControlsMixin):
    """Persist Studio state without selecting or wrapping a RuntimeAdapter."""

    def __init__(
        self,
        workspace: Workspace,
        executor: RuntimeExecutor,
        *,
        event_store: RunEventStore | None = None,
        session_service: BaseSessionService | None = None,
        runtime_events: RuntimeEventStore | None = None,
        plugin_runtime: Any | None = None,
        native_harness_ingress: bool = False,
    ) -> None:
        self.workspace = workspace
        self.executor = executor
        self.event_store = event_store or RunEventStore(workspace)
        self.session_service = session_service or LocalSessionService(
            project_dir=str(workspace.root)
        )
        self.runtime_events = runtime_events or RuntimeEventStore(self.session_service)
        self.plugin_runtime = plugin_runtime
        self.native_harness_ingress = native_harness_ingress
        self.schedule_assistant: Any | None = None
        self._active_handles: dict[str, Any] = {}
        self._active_sessions: set[tuple[str, str]] = set()
        # Serialize accepted next-turn prompts per Agent/Session. Direct Run
        # calls retain their overlap rejection; the asynchronous Studio
        # operation route may wait its turn without starting a second adapter.
        self._session_turn_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._session_turn_users: dict[tuple[str, str], int] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._control_queues: dict[
            str,
            asyncio.Queue[tuple[str, ResumePayload | None, asyncio.Future[None] | None]],
        ] = {}
        self._waiting_modes: dict[str, str] = {}
        self._submitted_interactions: dict[str, set[str]] = {}
        # 传输活性与持久化是两条独立合同。Harness 的 reasoning/text token
        # delta 出于隐私和体积原因不会写入 RunEventStore，但它们仍证明模型流
        # 在前进；看门狗必须观察这条进程内单调序号，不能把“未持久化”误判成
        # “没有收到任何 chunk”。
        self._runtime_progress_seq = 0
        # Interaction resolution appends to one run-level event file.  Serialise
        # every decision for that run, not only identical interaction ids, so
        # two concurrently visible cards cannot overwrite each other's receipt.
        self._interaction_locks: dict[str, asyncio.Lock] = {}
        self._recovery_tasks: set[asyncio.Task] = set()
        self._native_recovery_ready: dict[str, asyncio.Event] = {}
        self._detaching_recoveries = False
        # Idle wake for ordinary Native background commands. The dispatcher looks
        # up the per-watch Native adapter by (bundle_digest, session_id) from the
        # plugin runtime's native activations — no global adapter, no cross-Build
        # mixing. The coordinator reuses the existing ``start_notification_turn``
        # admission path; it does not copy recovery logic into the adapter.
        self._background_wakeup = NativeBackgroundWakeup(
            status_reader=self._background_command_status,
            is_idle=self._session_has_no_active_turn,
            dispatcher=_RunServiceWakeDispatcher(self),
            records_path=self.workspace.root / ".harness-tools" / "background"
            / "wakeup-records.json",
            poll_interval=0.5,
        )

    def has_active_runs(self) -> bool:
        # Accepted follow-ups remain live work even during the short handoff
        # between two Runs. Settings/credential changes must not slip through.
        return bool(self._session_turn_users or self._active_sessions or self._active_handles)

    def has_pending_session_turns(self, session_id: str) -> bool:
        return any(
            waiting_session == session_id and count > 0
            for (_, waiting_session), count in self._session_turn_users.items()
        )

    def _session_has_no_active_turn(self, agent_id: str, session_id: str) -> bool:
        # Session arbitration: never wake into an active turn. This preserves any
        # in-flight cancel/pause and matches the SESSION_RUN_ACTIVE admission gate.
        return self._session_turn_users.get((agent_id, session_id), 0) == 0

    async def _background_command_status(
        self, task_id: str, session_id: str, workspace_root: Path,
        command_state_root: Path, studio_run_id: str = "",
    ) -> str:
        # Resolve the actually-started backend from the source run's CANONICAL
        # launch receipt via the shared BackgroundCommandLocator (the same trusted
        # path the Native observer and the HTTP read route use), then read the
        # command status through make_background_session(backend=...) so E2B
        # background commands are observed on their own backend, not forced to a
        # local-only read. Never trust the request, the environment, or an
        # ambient key: the locator is fail-closed (unavailable) when the receipt
        # carries no provable host-supported backend.
        from ksadk.runtime_context import tool_execution_scope
        from ksadk.studio.native_background_locator import BackgroundCommandLocator
        from ksadk.toolsets.sandbox import make_background_session

        if not studio_run_id:
            return "unknown"
        studio = getattr(self, "_studio", None)
        if studio is None:
            # No owning StudioService bound — cannot resolve scope/store. Fail
            # closed rather than guessing a backend. service.py sets ``_studio``.
            return "unknown"
        resolved = await BackgroundCommandLocator(studio).resolve(studio_run_id, task_id)
        if resolved is None or resolved.backend is None:
            return "unknown"  # fail-closed: no provable host-supported backend
        composer = getattr(studio, "_native_product_composer", None)
        declaration = getattr(composer, "_background_declaration", None)
        with tool_execution_scope(session_id=session_id):
            session = make_background_session(
                workspace_root=workspace_root, state_root=command_state_root,
                backend=resolved.backend, declaration=declaration,
            )
            output = session.connect_background_command(task_id).read_output(max_bytes=8192)
        return str(output.status)

    async def _register_pending_background_commands(self, spec, record) -> None:
        # At the end of a completed ordinary Native run, watch every owned
        # background command — running or already terminal. A command that
        # completes during the final model turn may never have been observed at a
        # model boundary, so terminal at registration does NOT imply notified;
        # the coordinator registers all launched task_ids and the kernel's own
        # command_probe is the authority that skips already-notified commands in
        # the wake turn. Only harness runs launch background commands; the
        # ordinary Build compiler seals the notification policy together with the
        # command lifecycle, so a run with launched background commands carries
        # the observation policy.
        if record.status != RunStatus.COMPLETED or record.runtime_type != "harness":
            return
        handle = record.runtime_handle or {}
        native_run_id = handle.get("run_id") or handle.get("native_ref", {}).get("run_id")
        if not native_run_id:
            native_run_id = record.id
        workspace_root = self.workspace.root / ".harness-tools" / "workspace"
        command_state_root = self.workspace.root / ".harness-tools" / "background"
        task_ids = self._launched_background_task_ids(record.id)
        if not task_ids:
            return
        bundle_digest = str(spec.request_config.get("plugin_bundle_digest") or "")
        model = str(spec.model or "")
        approval_mode = str(spec.request_config.get("tool_approval_mode") or "")
        for task_id in task_ids:
            await self._background_wakeup.register(
                scope_id=str(spec.agent_id), session_id=record.session_id,
                build_id=spec.build_id, agent_id=spec.agent_id,
                source_run_id=native_run_id, native_run_id=native_run_id,
                task_id=task_id, bundle_digest=bundle_digest,
                workspace_root=workspace_root, command_state_root=command_state_root,
                studio_run_id=record.id, model=model, approval_mode=approval_mode,
            )

    def _launched_background_task_ids(self, run_id: str) -> list[str]:
        # Projected tool.completed events retain the background_task_id display
        # (background_output.read_background_output relies on the same shape).
        from ksadk.harness.tool_display_contract import safe_tool_display

        task_ids: list[str] = []
        for event in self.event_store.events(run_id):
            if event.type != "tool.completed":
                continue
            runtime_event = event.data.get("runtimeEvent") or {}
            source = runtime_event.get("source") or {}
            metadata = source.get("metadata") or {}
            if metadata.get("engine_kind") != "native":
                continue
            if metadata.get("tool_name") != "run_command":
                continue
            display = safe_tool_display(metadata.get("tool_display")) or {}
            task_id = display.get("background_task_id")
            if isinstance(task_id, str) and re.fullmatch(r"[a-f0-9]{64}", task_id):
                if task_id not in task_ids:
                    task_ids.append(task_id)
        return task_ids

    async def stop_background_wakeup(self) -> None:
        # Cancel the coordinator poll loop. The loop also self-exits when no
        # commands remain pending. StudioService.aclose calls this on shutdown.
        await self._background_wakeup.stop()

    async def start_background_wakeup(self) -> None:
        # Start the coordinator poll loop from the formal async service entry.
        # Construction (sync, no running loop) only queues recovered watches;
        # StudioService.start calls this so durable watches begin to fire
        # without waiting for a new user run.
        await self._background_wakeup.start()

    async def _project_notification_run(
        self, *, adapter, spec, source_record, source_run_id, task_id, handle,
    ) -> None:
        """Project the admitted Native turn through the ordinary public lifecycle."""
        run_id = f"run_{handle.run_id}"
        session = source_record.session_id
        if spec.plugin_bundle_root is not None:
            from ksadk.studio.plugin_kernel_adapter import StudioPluginKernelAdapter

            if self.plugin_runtime is None:
                raise ValueError("native_notification_plugin_runtime_unavailable")
            provider = self.plugin_runtime.kernel_adapter_provider(spec)()
            if not isinstance(provider, StudioPluginKernelAdapter):
                raise ValueError("native_notification_provider_changed")
            handle = await provider.prepare_native_notification(handle, expected_adapter=adapter)
            adapter = provider
        key = (spec.agent_id, session)
        # Use the same admission counter and lock as foreground user turns.
        # A notification observer must not clear another turn's ownership.
        lock = self._session_turn_locks.setdefault(key, asyncio.Lock())
        self._session_turn_users[key] = self._session_turn_users.get(key, 0) + 1
        try:
            async with lock:
                try:
                    record = self.event_store.get(run_id)
                except StudioError as exc:
                    if exc.status_code != 404:
                        raise
                    record = RunRecord(
                        id=run_id, build_id=spec.build_id, agent_id=spec.agent_id,
                        session_id=session, trace_id=uuid4().hex,
                        # A wake is a new run, not a second client submission receipt.
                        presentation_profile=source_record.presentation_profile,
                        native_runtime_options=source_record.native_runtime_options,
                        manifest_sha256=spec.manifest_sha256,
                        runtime_type=source_record.runtime_type,
                        model=str(spec.model or ""), input=source_record.input,
                    )
                    record.status = RunStatus.RUNNING
                    record.started_at = datetime.now(timezone.utc)
                    record.runtime_handle = handle.model_dump(mode="json")
                    self.event_store.create(record)
                    self.event_store.append(run_id, "run.created", {
                        "runId": run_id, "buildId": spec.build_id,
                        "sessionId": session, "traceId": record.trace_id,
                        "runtimeType": record.runtime_type,
                        "manifestSha256": spec.manifest_sha256, "model": record.model,
                        "notification": True, "sourceRunId": source_run_id,
                        "taskId": task_id,
                    })
                else:
                    if record.status not in {RunStatus.RUNNING, RunStatus.WAITING_INPUT,
                                             RunStatus.PAUSED}:
                        return
                    if run_id in self._active_handles:
                        return
                self._active_sessions.add(key)
                try:
                    handle = await self.executor.attach(
                        spec.launch_context, handle,
                        preparation=RuntimeStartPreparation(
                            context=spec.launch_context, adapter=adapter,
                        ),
                    )
                    await self._execute_runtime_record(
                        spec, source_record.input, record=record, restored_handle=handle,
                    )
                finally:
                    self._active_sessions.discard(key)
        finally:
            users = self._session_turn_users[key] - 1
            if users:
                self._session_turn_users[key] = users
            else:
                self._session_turn_users.pop(key, None)
                self._session_turn_locks.pop(key, None)

    def is_run_executing(self, run_id: str) -> bool:
        """该 run 是否仍在本进程内实际执行（内存态判定）。

        磁盘 record 停在 RUNNING 的 run 若不在这里，说明是进程崩溃/重启
        遗留的僵尸状态，允许删除会话时将其收尾为 canceled。
        """
        if run_id in self._active_handles:
            return True
        try:
            record = self.event_store.get(run_id)
        except StudioError:
            return False
        return (record.agent_id, record.session_id) in self._active_sessions

    async def reap_stale_running_run(self, run_id: str) -> bool:
        """把不在执行的僵尸 RUNNING run 收尾为 canceled（幂等）。"""
        from ksadk.events.canonical import RunCanceled

        record = self.event_store.get(run_id)
        if record.status != RunStatus.RUNNING or self.is_run_executing(run_id):
            return False
        record.status = RunStatus.CANCELLED
        record.error = {"code": "RUN_CANCELLED", "message": "会话删除时回收未完成的运行"}
        record.completed_at = datetime.now(timezone.utc)
        self.event_store.save(record)
        await self.runtime_events.append_one(
            record.session_id,
            RunCanceled(
                schema_version=2,
                event_id=f"evt_reap_{uuid4().hex}",
                seq=len(self.event_store.events(run_id)) + 1,
                timestamp=time.time(),
                run_id=run_id,
                scope_id=run_id,
                source=SourceRef(framework="ksadk", metadata={"reason": "session_deleted"}),
                status="canceled",
                reason="session deleted",
            ),
        )
        return True

    async def recover_interrupted(self, resolve_spec=None) -> None:
        """Settle local runs left active across a Studio restart.

        Reattach approval-paused Providers and opt-in local Native runs only
        when their pinned Build and durable-restore capability can be verified.
        Other in-process runs are explicitly interrupted; hosted takeover
        remains Kernel-owned.
        """
        active = {RunStatus.RUNNING, RunStatus.WAITING_INPUT, RunStatus.PAUSED}
        for record in self.event_store.list_runs():
            if record.status == RunStatus.COMPLETED:
                from ksadk.studio.native_terminal_recovery import rebuild_native_terminal_trace

                await rebuild_native_terminal_trace(self, record, resolve_spec)
            if record.status not in active:
                continue
            native_ref = (
                record.runtime_handle.get("native_ref", {}) if record.runtime_handle else {}
            )
            is_native = isinstance(native_ref, dict) and native_ref.get("engine_kind") == "native"
            if (
                record.status == RunStatus.WAITING_INPUT
                and resolve_spec is not None
                and not is_native
            ):
                from ksadk.studio.provider_recovery import restore_waiting_provider_run

                if await restore_waiting_provider_run(self, record, resolve_spec):
                    continue
            if resolve_spec is not None:
                from ksadk.studio.native_recovery import restore_native_studio_run

                if await restore_native_studio_run(self, record, resolve_spec):
                    continue
            record.status = RunStatus.INTERRUPTED
            record.completed_at = datetime.now(timezone.utc)
            record.error = {
                "code": "LOCAL_STUDIO_RESTARTED",
                "message": "本地 Studio 重启，运行未在本地恢复。",
            }
            self.event_store.save(record)
            self.event_store.append(
                record.id,
                "run.interrupted",
                {"reason": "local_studio_restarted", "recoverable": False},
            )
            self._expire_unresolved_interactions(record)

    async def events(
        self, run_id: str, *, after: int = 0, presentation_profile: str | None = None
    ) -> list[RunEvent]:
        """Read the durable Studio event timeline for API/SSE replay."""
        from ksadk.studio.presentation import StudioConversationProjection

        record = self.event_store.get(run_id)
        if presentation_profile is not None and presentation_profile != record.presentation_profile:
            raise StudioError(
                "PRESENTATION_PROFILE_CONFLICT", "重连必须沿用当前运行的展示方式", status_code=409
            )
        projector = StudioConversationProjection(record.session_id, record.presentation_profile)
        result = []
        for event in self.event_store.events(run_id, after=0):
            projected = projector.apply(event)
            if event.id > after:
                result.append(projected)
        return result

    async def run(
        self,
        spec: StudioRunSpec,
        user_input: str,
        *,
        runtime_input: Any = None,
        session_id: str | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
        queue_if_active: bool = False,
        on_admitted: Callable[[], None] | None = None,
    ) -> RunRecord:
        session = session_id or f"ses_{uuid4().hex}"
        if spec.submission_ref is not None and (
            spec.submission_ref.agent_id != spec.agent_id
            or spec.submission_ref.session_id != session
        ):
            raise StudioError(
                "RUN_SUBMISSION_SCOPE_MISMATCH", "提交来源与实际运行不一致", status_code=409
            )
        key = (spec.agent_id, session)
        # Count the admission before the first await. A direct caller cannot
        # jump ahead of an already accepted follow-up during lock handoff.
        if not queue_if_active and self._session_turn_users.get(key, 0):
            raise StudioError(
                "SESSION_RUN_ACTIVE",
                "上一轮仍在运行或等待回答，请先完成回答或停止运行。",
                status_code=409,
                details={"sessionId": session},
            )
        # Accepted structured input belongs to this submission, not to the
        # mutable composer/caller while this coroutine waits for its turn.
        runtime_input = copy.deepcopy(runtime_input)
        lock = self._session_turn_locks.setdefault(key, asyncio.Lock())
        self._session_turn_users[key] = self._session_turn_users.get(key, 0) + 1
        try:
            async with lock:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    raise asyncio.CancelledError()
                if on_admitted is not None:
                    on_admitted()
                self._active_sessions.add(key)
                watchdog = asyncio.create_task(self._watch_run_progress())
                try:
                    return await self._run_owned(
                        spec,
                        user_input,
                        runtime_input=runtime_input,
                        session_id=session,
                        on_event=on_event,
                    )
                finally:
                    watchdog.cancel()
                    with suppress(asyncio.CancelledError):
                        await watchdog
                    self._active_sessions.discard(key)
        finally:
            users = self._session_turn_users[key] - 1
            if users:
                self._session_turn_users[key] = users
            else:
                self._session_turn_users.pop(key, None)
                self._session_turn_locks.pop(key, None)

    async def _watch_run_progress(self) -> None:
        """运行期"无进展"看门狗：只做取证，不改行为。

        依据：2026-09-18 实测一次 run 静默 437s、期间 0 事件，而进程 CPU 稳定
        在 ~20%（空转基线 0.3%），日志里没有任何一行能说明"当时在干什么"；
        排查只能靠 `lsof` 反推，且 macOS 上 py-spy 需要 root、lldb attach 被拒。
        因此把"疑似卡住"时的现场（asyncio 任务栈）直接打进 studio 日志：
        下次再卡住，根因在日志里，不需要复现第二次。

        阈值：``KSADK_RUN_STALL_WARN_SECONDS``（默认 60，<=0 关闭）；
        现场 dump 从 ``KSADK_RUN_STALL_DUMP_SECONDS``（默认 120）开始，
        **持续卡住就每隔这么久再 dump 一次**（最多 ``KSADK_RUN_STALL_MAX_DUMPS``
        次，默认 5）。一次性 dump 区分不了"卡住"和"只是慢"：2026-09-18 实测一次
        run 静默 10 分钟、期间零模型请求、进程全部线程 idle，而 21:36:08 那份
        一次性 dump 早已过期，无法说明 6 分钟后的状态。
        """

        warn_after = _stall_seconds("KSADK_RUN_STALL_WARN_SECONDS", 60.0)
        if warn_after <= 0:
            return
        dump_after = _stall_seconds("KSADK_RUN_STALL_DUMP_SECONDS", 120.0)
        max_dumps = int(_stall_seconds("KSADK_RUN_STALL_MAX_DUMPS", 5.0))
        poll_seconds = min(15.0, warn_after)
        quiet_since = time.monotonic()
        last_seq = self._progress_marker()
        warned = False
        dumps = 0
        last_dump_at = 0.0
        while True:
            await asyncio.sleep(poll_seconds)
            marker = self._progress_marker()
            if marker != last_seq:
                last_seq = marker
                quiet_since = time.monotonic()
                warned = False
                dumps = 0
                last_dump_at = 0.0
                continue
            quiet_for = time.monotonic() - quiet_since
            if quiet_for >= warn_after and not warned:
                warned = True
                logger.warning(
                    "run 已 %.0fs 没有新事件（当前执行中 run=%s）；疑似卡住，"
                    "达到 %.0fs 将 dump asyncio 任务栈",
                    quiet_for,
                    sorted(self._active_handles) or sorted(self._active_sessions),
                    dump_after,
                )
            due_for_dump = quiet_for >= dump_after and (
                last_dump_at == 0.0 or quiet_for - last_dump_at >= dump_after
            )
            if due_for_dump and dumps < max_dumps:
                dumps += 1
                last_dump_at = quiet_for
                logger.warning(
                    "run 已 %.0fs 没有新事件（第 %d/%d 次现场）；"
                    "asyncio 任务现场：\n%s\n事件循环最近的定时器：\n%s",
                    quiet_for,
                    dumps,
                    max_dumps,
                    _format_task_stacks(),
                    _format_loop_timers(),
                )

    def _progress_marker(self) -> tuple[int, int, int]:
        """进展标记：持久事件、活跃会话与未落盘的实时流序号。"""

        total = 0
        for run_id in list(self._active_handles):
            try:
                total += len(self.event_store.events(str(run_id)))
            except Exception:  # noqa: BLE001 - 取证代码不得影响主流程
                continue
        return total, len(self._active_sessions), self._runtime_progress_seq


    async def _start_public_execution(self, run_id, launch_context, request, preparation):
        from ksadk.harness.public_output import public_output_scope
        from ksadk.studio.public_output_store import PublicOutputStore

        store = PublicOutputStore(self.workspace)
        with public_output_scope(lambda text, **kwargs: store.retain(run_id, text, **kwargs)):
            return await self.executor.start(launch_context, request, preparation=preparation)

    async def _public_execution_stream(self, handle, run_id):
        from ksadk.harness.public_output import public_output_scope
        from ksadk.studio.public_output_store import PublicOutputStore

        store = PublicOutputStore(self.workspace)
        def sink(text, **kwargs):
            return store.retain(run_id, text, **kwargs)

        iterator = self.executor.stream(handle)
        try:
            while True:
                with public_output_scope(sink):
                    try:
                        event = await anext(iterator)
                    except StopAsyncIteration:
                        return
                yield event
        finally:
            await iterator.aclose()

    async def _run_owned(
        self,
        spec: StudioRunSpec,
        user_input: str,
        *,
        runtime_input: Any,
        session_id: str,
        on_event: Callable[[RunEvent], None] | None,
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
            submission_ref=spec.submission_ref,
            manifest_sha256=spec.manifest_sha256,
            runtime_type=runtime_type,
            model=str(spec.model or ""),
            collaboration_mode=str(spec.request_config.get("collaboration_mode") or ""),
            goal_objective=str(spec.request_config.get("goal_objective") or ""),
            presentation_profile=spec.presentation_profile,
            input=user_input,
        )
        await self.session_service.create_session(spec.agent_id, "local-user", session)
        self.event_store.create(record)
        prepared_turn = await self._capture_pcm_evidence(record, spec, user_input)
        created = self.event_store.append(
            record.id,
            "run.created",
            {
                "runId": record.id,
                "buildId": spec.build_id,
                "sessionId": session,
                "traceId": record.trace_id,
                "runtimeType": runtime_type,
                "manifestSha256": spec.manifest_sha256,
                "model": record.model,
                "collaborationMode": record.collaboration_mode,
                "goalObjective": record.goal_objective,
                "presentationProfile": record.presentation_profile,
            },
        )
        if on_event is not None:
            on_event(created)

        if self.schedule_assistant is not None and (
            self.schedule_assistant.matches(user_input)
            or self.schedule_assistant.matches_followup(spec.agent_id, session, user_input)
        ):
            handled = await self._schedule_run(record, user_input, on_event=on_event)
            if handled is not None:
                return handled

        provider_runtime_adapter = (
            spec.plugin_bundle_root is not None
            and (
                runtime_type == "codex"
                or bool(spec.request_config.get("provider_runtime_adapter"))
            )
        )
        # A process-global Codex Kernel can share Agent/cwd with this Build
        # while owning a legacy direct factory. New Builds must retain their
        # exact Provider activation, including when that global Kernel exists.
        kernel_runtime = (
            None if provider_runtime_adapter else self._kernel_runtime_for_spec(spec)
        )
        if kernel_runtime is not None:
            return await self._kernel_run(
                spec,
                user_input,
                record=record,
                on_event=on_event,
                kernel_runtime=kernel_runtime,
            )
        if spec.plugin_bundle_root is not None and not provider_runtime_adapter:
            return await self._plugin_run(
                spec,
                user_input,
                record=record,
                on_event=on_event,
            )

        return await self._execute_runtime_record(
            spec, user_input, record=record, runtime_input=runtime_input,
            on_event=on_event, prepared_turn=prepared_turn,
            provider_runtime_adapter=provider_runtime_adapter,
        )

    def _admit_native_studio_start(
        self,
        spec: StudioRunSpec,
        record: RunRecord,
        user_input: str,
        request: StartRequest,
        adapter: Any,
        *,
        require_pin: bool,
    ) -> StartRequest:
        """Apply the same Build/history fence to direct and PluginHost Native starts."""
        adapter.admit_studio_build(
            build_id=spec.build_id,
            manifest_sha256=spec.manifest_sha256,
            model=request.model,
            request_config=request.config,
            project_dir=spec.launch_context.project_dir,
            launch_config=spec.launch_context.config,
            require_pin=require_pin,
        )
        from ksadk.studio.native_user_input import admit_native_user_input

        normalized_input = admit_native_user_input(request.input, user_input)
        prior_turns = [
            previous
            for previous in self.event_store.list_runs(session_id=record.session_id)
            if previous.id != record.id
            and previous.agent_id == spec.agent_id
            and (
                previous.status == RunStatus.COMPLETED
                or (
                    previous.status
                    in {
                        RunStatus.FAILED,
                        RunStatus.CANCELLED,
                        RunStatus.INTERRUPTED,
                        RunStatus.TIMED_OUT,
                    }
                    and previous.runtime_handle.get("run_id")
                )
            )
        ]
        parent = adapter.session_parent_run_id(record.session_id)
        if prior_turns:
            last = prior_turns[-1]
            if (
                any(
                    (previous.runtime_handle.get("native_ref") or {}).get("engine_kind")
                    != "native"
                    or previous.build_id != spec.build_id
                    or previous.manifest_sha256 != spec.manifest_sha256
                    for previous in prior_turns
                )
                or parent != last.runtime_handle.get("run_id")
            ):
                raise ValueError("native_studio_cross_runtime_history_unavailable")
        elif parent is not None:
            raise ValueError("native_studio_session_parent_mismatch")
        # The Native ledger is the only history owner. The fields removed here
        # have already been checked against immutable host-side admissions.
        from ksadk.studio.contracts import NativeRunOptions

        if spec.plugin_bundle_root is not None:
            # Ordinary Studio/DSH owns these normalized selections. Explicit
            # injected hosts retain their separately pinned request contract;
            # their opaque policy names are not Studio approval vocabulary.
            record.native_runtime_options = NativeRunOptions(
                tool_approval_mode=spec.request_config.get("tool_approval_mode"),
                reasoning_effort=spec.request_config.get("effort") or None,
            )
            self.event_store.save(record)
        return request.model_copy(update={
            "metadata": {}, "config": {}, "model": None, "input": normalized_input,
        })

    async def _execute_runtime_record(
        self, spec, user_input, *, record, runtime_input=None, on_event=None,
        prepared_turn=None, provider_runtime_adapter=False, restored_handle=None,
    ) -> RunRecord:
        run_id, session = record.id, record.session_id
        runtime_type = record.runtime_type
        started = time.monotonic()
        if restored_handle is None:
            record.status = RunStatus.RUNNING
            record.started_at = datetime.now(timezone.utc)
        self.event_store.save(record)
        projected_writer = _ProjectedRunEventWriter(self.event_store, run_id, on_event)
        native_restore = (
            restored_handle is not None
            and restored_handle.native_ref.get("engine_kind") == "native"
        )
        native_restored_run_id = restored_handle.run_id if native_restore else None
        existing_events = self.event_store.events(run_id) if native_restore else ()
        # A cold Native replay reconstructs record state from the full ledger.
        # Studio's public event file is a separate projection; its persisted
        # run_seq is the safe prefix, including batches of prior text deltas.
        public_replay_cursor = max(
            (
                fact.get("run_seq", 0)
                for row in existing_events
                if isinstance((fact := row.data.get("runtimeEvent")), dict)
                and fact.get("run_id") == native_restored_run_id
                and type(fact.get("run_seq")) is int
            ),
            default=0,
        )
        approval_surfaces = {
            str(row.data.get("interactionId"))
            for row in existing_events
            if row.type == "a2ui.interaction" and row.data.get("interactionId")
        }

        async def persist(
            runtime_event: RuntimeEvent, *, terminal_record: RunRecord | None = None,
        ) -> RunEvent | None:
            # 必须早于 private-delta 过滤：对齐 Codex/Pi/OpenClaw 的 transport
            # liveness 语义，任意真实应用层事件都续期；是否持久化/展示由下面的
            # 投影策略单独决定。
            self._runtime_progress_seq += 1
            if _is_private_harness_delta(runtime_event):
                return None
            persisted = await self.runtime_events.append_one(record.session_id, runtime_event)
            if (
                native_restore
                and persisted.run_id == native_restored_run_id
                and persisted.run_seq is not None
                and persisted.run_seq <= public_replay_cursor
            ):
                return None
            event_type, data = project_runtime_event(
                persisted,
                session_id=record.session_id,
                public_run_id=record.id,
            )
            if isinstance(persisted, InteractionRequested):
                data["revision"] = 1
                conversation_item = data.get("conversationItem")
                if isinstance(conversation_item, dict):
                    item_payload = conversation_item.get("payload")
                    if isinstance(item_payload, dict):
                        item_payload["revision"] = 1
            return projected_writer.append(event_type, data, record=terminal_record)

        handle = None
        completed_text_by_item: dict[tuple[str, str], str] = {}
        streamed_text_by_item: dict[tuple[str, str], str] = {}
        terminal_output_refs: tuple[OutputRef, ...] = ()
        terminal_notice = ""
        pending_completed_event: RunCompleted | None = None
        runtime_duration_ms: int | None = None
        native_terminal_metadata_fixed = False
        item_phases: dict[tuple[str, str], str | None] = {}
        control_queue: asyncio.Queue[
            tuple[str, ResumePayload | None, asyncio.Future[None] | None]
        ] = asyncio.Queue()
        self._control_queues[run_id] = control_queue
        cancellation_reason = "cancelled"
        try:
            tool_approval_mode = str(spec.request_config.get("tool_approval_mode") or "")
            conversation_request: dict[str, Any] = {
                "messages": self._conversation_messages(
                    spec.agent_id,
                    session,
                    user_input,
                )
            }
            if tool_approval_mode:
                conversation_request["request_metadata"] = {
                    "tool_approval_mode": tool_approval_mode,
                }
            if prepared_turn is not None:
                conversation_request["prepared_turn"] = asdict(prepared_turn)
            request = StartRequest(
                input=runtime_input if runtime_input is not None else user_input,
                user_id="local-user",
                session_id=session,
                agent_id=spec.agent_id,
                model=spec.model,
                config=dict(spec.request_config),
                metadata={
                    "invocation_id": run_id,
                    CONVERSATION_PREPROCESSING_METADATA_KEY: conversation_request,
                    **self._native_session_metadata(
                        spec.agent_id,
                        session,
                        str(
                            spec.request_config.get("provider_runtime_type")
                            or runtime_type
                        ),
                        spec.build_id,
                    ),
                },
            )
            preparation = None
            if restored_handle is None and provider_runtime_adapter:
                if self.plugin_runtime is None:
                    raise StudioError(
                        "PLUGIN_RUNTIME_UNAVAILABLE", "Codex Provider Runtime 不可用",
                        status_code=503,
                    )
                adapter = self.plugin_runtime.kernel_adapter_provider(spec)()
                from ksadk.studio.plugin_kernel_adapter import StudioPluginKernelAdapter

                if isinstance(adapter, StudioPluginKernelAdapter):
                    adapter.bind_native_admission(
                        lambda native, bound_request: self._admit_native_studio_start(
                            spec,
                            record,
                            user_input,
                            bound_request,
                            native,
                            require_pin=True,
                        )
                    )
                preparation = RuntimeStartPreparation(context=spec.launch_context, adapter=adapter)
            elif (
                restored_handle is None
                and self.native_harness_ingress
                and runtime_type == "harness"
            ):
                # Keep the selected adapter for this one start. Admission must
                # run before Native compiles or launches any effectful port.
                preparation = RuntimeStartPreparation(
                    context=spec.launch_context,
                    adapter=self.executor.create_adapter(spec.launch_context),
                )
                from ksadk.harness.native_runtime import NativeRuntimeAdapter

                if isinstance(preparation.adapter, NativeRuntimeAdapter):
                    request = self._admit_native_studio_start(
                        spec,
                        record,
                        user_input,
                        request,
                        preparation.adapter,
                        require_pin=False,
                    )
            handle = restored_handle or await self._start_public_execution(
                run_id, spec.launch_context, request, preparation,
            )
            record.runtime_handle = handle.model_dump(mode="json")
            self.event_store.save(record)
            self._active_handles[run_id] = handle
            approval_timeout = spec.request_config.get("approval_timeout_seconds")
            approval_wait_seconds = (
                float(approval_timeout) if approval_timeout is not None else None
            )
            if approval_wait_seconds is not None and approval_wait_seconds <= 0:
                raise ValueError("approval_timeout_seconds must be positive")
            approval_deadline: float | None = None
            while True:
                terminal_seen = False
                should_resume = restored_handle is not None
                if restored_handle is not None:
                    self._waiting_modes[run_id] = "resume"
                    restored_handle = None
                async for event in self._public_execution_stream(handle, run_id):
                    if (
                        isinstance(event, RunInterrupted)
                        and event.interaction_id
                        and record.status == RunStatus.WAITING_INPUT
                        and event.source.framework == "codex"
                    ):
                        # Codex emits this companion event immediately after a
                        # live JSON-RPC approval request. The native turn stays
                        # attached and must be released through submit; trying
                        # to resume it deadlocks that request and later breaks
                        # the app-server pipe.
                        self._waiting_modes[run_id] = "live"
                        continue
                    if isinstance(event, InteractionResolved) and (
                        event.source.framework == "codex"
                        or event.source.metadata.get("engine_kind") == "native"
                    ):
                        submitted = self._submitted_interactions.get(run_id, set())
                        provider_call_id = str(getattr(event.response, "call_id", None) or "")
                        if event.interaction_id in submitted or provider_call_id in submitted:
                            # submit_interaction atomically persists the public
                            # terminal fact and receipt. Ignore the provider's
                            # echo so replay never shows the approval twice.
                            if event.source.metadata.get("engine_kind") == "native":
                                # Keep the Native canonical fact for replay and
                                # reconciliation; the Studio action already
                                # wrote its public approval.resolved row.
                                await self.runtime_events.append_one(record.session_id, event)
                            continue
                    # ``run.completed`` is only a provider claim until Studio has
                    # resolved its output refs and applied the delivery gate.  If
                    # we persist it first and the gate then rejects an empty
                    # delivery, replay contains both ``run.completed`` and
                    # ``run.failed`` for one run.  Buffer the successful terminal
                    # fact; all non-success events remain live and durable.
                    if isinstance(event, RunCompleted):
                        pending_completed_event = event
                    else:
                        await persist(event)
                    if self._cancel_flags.get(run_id):
                        raise asyncio.CancelledError()
                    if isinstance(event, ItemStarted) and event.item_kind == "message":
                        item_phases[(event.scope_id, event.item_id)] = event.phase
                    elif (
                        isinstance(event, ItemUpdated)
                        and event.item_kind == "message"
                        and item_phases.get((event.scope_id, event.item_id), "final_answer")
                        == "final_answer"
                    ):
                        text = event.update.text if isinstance(event.update, TextContent) else ""
                        item_key = (event.scope_id, event.item_id)
                        if event.op == "replace":
                            streamed_text_by_item[item_key] = text
                        else:
                            streamed_text_by_item[item_key] = (
                                streamed_text_by_item.get(item_key, "") + text
                            )
                    elif (
                        isinstance(event, ItemCompleted)
                        and event.item_kind == "message"
                        and item_phases.get((event.scope_id, event.item_id), "final_answer")
                        == "final_answer"
                    ):
                        completed_text_by_item[(event.scope_id, event.item_id)] = "".join(
                            part.text
                            for part in event.snapshot.parts
                            if isinstance(part, TextContent)
                        )
                    elif isinstance(event, UsageReported):
                        _apply_usage_event(record, event, runtime_type)
                    elif isinstance(event, InteractionRequested):
                        record.status = RunStatus.WAITING_INPUT
                        approval_deadline = (
                            asyncio.get_running_loop().time() + approval_wait_seconds
                            if event.interaction_kind == "approval"
                            and approval_wait_seconds is not None
                            else None
                        )
                        native_interaction = event.source.metadata.get("engine_kind") == "native"
                        # Both Managed LangGraph and Native Harness suspend the
                        # stream and accept approvals through resume(). A user
                        # may submit as soon as the request is published,
                        # before the companion RunInterrupted is projected.
                        # Mark that route now instead of briefly sending an
                        # early approval to RuntimeAdapter.submit() (unsupported
                        # by Managed Harness). Codex keeps its live JSON-RPC
                        # submit channel.
                        resume_interaction = native_interaction or runtime_type == "harness"
                        self._waiting_modes[run_id] = (
                            "resume" if resume_interaction else "live"
                        )
                        # Native stream ends at the durable suspension. Its
                        # continuation is a new resume call on the same run.
                        should_resume = should_resume or resume_interaction
                        self.event_store.save(record)
                        if (
                            event.interaction_kind == "approval"
                            and event.interaction_id not in approval_surfaces
                        ):
                            self._persist_approval_surface(record, event, on_event=on_event)
                            approval_surfaces.add(event.interaction_id)
                    elif isinstance(event, InteractionResolved):
                        record.status = RunStatus.RUNNING
                        approval_deadline = None
                        self._waiting_modes.pop(run_id, None)
                        self.event_store.save(record)
                    elif isinstance(event, RunFailed):
                        terminal_seen = True
                        record.status = RunStatus.FAILED
                        record.error = {
                            "code": "RUNTIME_RUN_FAILED",
                            "message": str(event.error.message or "Runtime 运行失败"),
                        }
                    elif isinstance(event, RunCanceled):
                        terminal_seen = True
                        record.status = RunStatus.CANCELLED
                        record.error = {
                            "code": "RUN_CANCELLED",
                            "message": str(event.reason or "运行已取消"),
                        }
                    elif isinstance(event, RunInterrupted):
                        if _is_pause_interruption(event):
                            record.status = RunStatus.PAUSED
                            should_resume = True
                        elif record.status == RunStatus.WAITING_INPUT:
                            self._waiting_modes[run_id] = "resume"
                            should_resume = True
                        else:
                            terminal_seen = True
                            record.status = RunStatus.INTERRUPTED
                            record.error = {
                                "code": "RUN_INTERRUPTED",
                                "message": event.reason or "运行已中断",
                            }
                        self.event_store.save(record)
                    elif isinstance(event, RunCompleted):
                        terminal_seen = True
                        if event.source.metadata.get("engine_kind") == "native":
                            # A timer can flush the Store's cached record while
                            # canonical persistence awaits. Keep that old record
                            # active until the explicit public terminal commit.
                            record = record.model_copy(deep=True)
                        record.status = RunStatus.COMPLETED
                        if event.source.metadata.get("engine_kind") == "native":
                            terminal_notice = terminal_verification_notice(event.source.metadata)
                            native_outcome = event.source.metadata.get("outcome")
                            if isinstance(native_outcome, str) and native_outcome in {
                                "verified", "unverified", "partial", "blocked"
                            }:
                                record.verification_outcome = native_outcome
                        terminal_output_refs = event.output_refs
                        raw_duration = event.source.metadata.get("duration_ms")
                        if raw_duration is None:
                            metrics = event.source.metadata.get("metrics")
                            if isinstance(metrics, dict):
                                raw_duration = metrics.get("duration_ms")
                        if raw_duration is not None:
                            runtime_duration_ms = max(0, int(raw_duration))
                if terminal_seen:
                    break
                if not should_resume:
                    record.status = RunStatus.COMPLETED
                    break

                # Cold replay may include older, already resolved cards. Only
                # signal readiness after the full stream reaches its current
                # durable suspension and this loop can consume resume commands.
                recovery_ready = self._native_recovery_ready.pop(run_id, None)
                if recovery_ready is not None:
                    recovery_ready.set()
                try:
                    if record.status == RunStatus.WAITING_INPUT and approval_deadline is not None:
                        command, resume_payload, submit_ack = await asyncio.wait_for(
                            control_queue.get(),
                            timeout=max(
                                0.0,
                                approval_deadline - asyncio.get_running_loop().time(),
                            ),
                        )
                    else:
                        command, resume_payload, submit_ack = await control_queue.get()
                except TimeoutError:
                    cancellation_reason = "approval_timeout"
                    self._cancel_flags[run_id] = True
                    raise asyncio.CancelledError() from None
                if command == "cancel" or self._cancel_flags.get(run_id):
                    raise asyncio.CancelledError()
                if command != "resume":
                    continue
                approval_deadline = None
                native_thread_id = handle.native_ref.get("thread_id")
                target = (
                    ResumeTarget(kind="thread_id", id=str(native_thread_id))
                    if native_thread_id
                    else ResumeTarget(kind="invocation_id", id=handle.run_id)
                )
                try:
                    handle = await self.executor.resume(
                        handle,
                        target,
                        resume_payload,
                    )
                except Exception as exc:
                    if submit_ack is not None and not submit_ack.done():
                        submit_ack.set_exception(exc)
                    raise
                if submit_ack is not None and not submit_ack.done():
                    submit_ack.set_result(None)
                self._active_handles[run_id] = handle
                record.runtime_handle = handle.model_dump(mode="json")
                record.status = RunStatus.RUNNING
                record.error = None
                self._waiting_modes.pop(run_id, None)
                resumed = self.event_store.append(
                    run_id,
                    "run.resumed",
                    {"runId": run_id, "runtimeHandle": record.runtime_handle},
                )
                if on_event is not None:
                    on_event(resumed)
                self.event_store.save(record)
            text_by_item = {**streamed_text_by_item, **completed_text_by_item}
            if terminal_output_refs:
                terminal_parts = [
                    text_by_item.get((ref.scope_id, ref.item_id), "")
                    for ref in terminal_output_refs
                ]
                record.output = "\n\n".join(part for part in terminal_parts if part)
            else:
                completed_parts = [part for part in completed_text_by_item.values() if part]
                fallback_parts = [part for part in streamed_text_by_item.values() if part]
                record.output = "\n\n".join(completed_parts or fallback_parts)
            if record.status == RunStatus.COMPLETED and (
                _consumed_model_output(record)
                or (handle is not None and handle.native_ref.get("engine_kind") == "native")
            ):
                # 空交付不得判 COMPLETED。harness 的 **provider adapter** 路径就走这里
                # （runtimeHandle 里 `_ksadk_plugin_provider_runtime_type: harness`），
                # 我前两版守卫分别错放在 `_schedule_run` 和只盖 `_plugin_run`，于是
                # run_4abd9ab7 仍然以 COMPLETED + output 长度 0 + 产物 0 收场。
                #
                # 收窄条件：只有**确实消耗了模型输出 token** 才算"空交付"。审批/交互
                # 这类合法地以空输出收束的流程不应被判失败（实测有 3 个审批用例因此挂）。
                _require_delivery(record.output)
            if record.status == RunStatus.COMPLETED and record.output and terminal_notice:
                record.output += terminal_notice
            if pending_completed_event is not None:
                native_terminal = (
                    handle is not None and handle.native_ref.get("engine_kind") == "native"
                    and pending_completed_event.source.metadata.get("engine_kind") == "native"
                )
                if native_terminal:
                    # Seal the host's completion clock with the public terminal,
                    # before cleanup can be interrupted by process termination.
                    record.completed_at = datetime.now(timezone.utc)
                    record.duration_ms = (
                        runtime_duration_ms if runtime_duration_ms is not None
                        else int((time.monotonic() - started) * 1000)
                    )
                    record.duration_source = (
                        "runtime" if runtime_duration_ms is not None else "studio"
                    )
                    native_terminal_metadata_fixed = True
                await persist(
                    pending_completed_event, terminal_record=record if native_terminal else None,
                )
        except asyncio.CancelledError:
            if self._detaching_recoveries and record.status == RunStatus.WAITING_INPUT:
                raise
            cancel_result = "task_cancelled"
            if handle is not None and self.executor.is_attached(handle):
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(self.executor.cancel(handle)),
                        timeout=_CANCEL_TIMEOUT_SECONDS,
                    )
                    cancel_result = result.value
                except TimeoutError:
                    cancel_result = "cancel_timed_out"
                except Exception:  # best effort; close still owns resource cleanup
                    cancel_result = "cancel_failed"
            record.status = RunStatus.CANCELLED
            record.error = (
                {"code": "APPROVAL_TIMEOUT", "message": "审批等待超时，运行已取消"}
                if cancellation_reason == "approval_timeout"
                else {"code": "RUN_CANCELLED", "message": "运行已取消"}
            )
            cancelled_run_id = handle.run_id if handle is not None else run_id
            # A provider may emit its own canceled terminal immediately before
            # the host's cancel flag raises. Preserve that canonical fact;
            # synthesizing another one would give this Run two terminals.
            already_cancelled = any(
                event.type == "run.cancelled"
                for event in self.event_store.events(run_id)
            )
            if not already_cancelled:
                cancelled = RunCanceled(
                    schema_version=2,
                    event_id=f"evt_cancel_{uuid4().hex}",
                    seq=len(self.event_store.events(run_id)) + 1,
                    timestamp=time.time(),
                    run_id=cancelled_run_id,
                    scope_id=cancelled_run_id,
                    source=SourceRef(
                        framework="ksadk",
                        metadata={"cancel_result": cancel_result},
                    ),
                    status="canceled",
                    reason=cancellation_reason,
                )
                await persist(cancelled)
            raise
        except Exception as exc:  # noqa: BLE001
            record.status = RunStatus.FAILED
            record.error = {"code": "RUNTIME_RUN_FAILED", "message": str(exc)}
            from ksadk.harness.native_driver_failure import (
                NativeDriverFailure,
                NativeDriverFailureDiagnostic,
            )

            diagnostic_metadata = {}
            if (type(exc) is NativeDriverFailure
                    and type(exc.diagnostic) is NativeDriverFailureDiagnostic):
                diagnostic_metadata["native_driver_failure"] = exc.diagnostic.model_dump(
                    mode="json"
                )
                record.error["details"] = diagnostic_metadata
            failed_run_id = handle.run_id if handle is not None else run_id
            failure = RunFailed(
                schema_version=2,
                event_id=f"evt_fail_{uuid4().hex}",
                seq=len(self.event_store.events(run_id)) + 1,
                timestamp=time.time(),
                run_id=failed_run_id,
                scope_id=failed_run_id,
                source=SourceRef(framework="ksadk", metadata=diagnostic_metadata),
                status="failed",
                error=ErrorInfo(
                    code="RUNTIME_RUN_FAILED",
                    message=str(exc),
                    source="ksadk",
                    scope_id=failed_run_id,
                ),
            )
            await persist(failure)
        finally:
            projected_writer.flush()
            if handle is not None and self.executor.is_attached(handle):
                if handle.native_ref.get("engine_kind") == "native":
                    # Persist curated child history before the activation closes.
                    # UI subscriptions are observers and cannot keep it alive.
                    try:
                        children = await self.execution_children(run_id, include_details=True)
                        for child in children:
                            if child["status"] in {"running", "paused"}:
                                child["lastObservedStatus"] = child["status"]
                                child["status"] = "unknown"
                                for message in child.get("messages", []):
                                    message["streaming"] = False
                                for fact in child.get("facts", []):
                                    if fact.get("status") == "running":
                                        fact["status"] = "unknown"
                                    for tool in fact.get("tools", []):
                                        if tool.get("status") == "running":
                                            tool["status"] = "unknown"
                        if children:
                            self.event_store.append(
                                run_id, "child.execution_snapshot", {"children": children}
                            )
                    except Exception as exc:
                        # Optional presentation must never skip activation cleanup.
                        logger.warning(
                            "Child execution snapshot unavailable: %s", type(exc).__name__
                        )
                try:
                    await asyncio.shield(self.executor.close(handle))
                except asyncio.CancelledError:
                    pass
                except Exception:  # cleanup is best effort; run state is already durable
                    pass
            self._active_handles.pop(run_id, None)
            self._cancel_flags.pop(run_id, None)
            self._control_queues.pop(run_id, None)
            self._waiting_modes.pop(run_id, None)
            self._submitted_interactions.pop(run_id, None)
            preserve_waiting = (
                self._detaching_recoveries and record.status == RunStatus.WAITING_INPUT
            )
            preserve_terminal_metadata = (
                native_terminal_metadata_fixed and record.status == RunStatus.COMPLETED
            )
            if not preserve_waiting:
                if not preserve_terminal_metadata:
                    record.completed_at = datetime.now(timezone.utc)
                self._expire_unresolved_interactions(record, on_event=on_event)
            if not preserve_terminal_metadata:
                if runtime_duration_ms is not None:
                    record.duration_ms = runtime_duration_ms
                    record.duration_source = "runtime"
                else:
                    record.duration_ms = int((time.monotonic() - started) * 1000)
                    record.duration_source = "studio"
            self.event_store.save(record)
            await self._sync_trace(record)
        # Register any still-running background commands for idle wake. Best
        # effort: never blocks the run result. Reuses the existing notification
        # admission path; only harness runs launch background commands.
        try:
            if record.status == RunStatus.COMPLETED:
                await self._register_pending_background_commands(spec, record)
        except Exception as exc:
            logger.warning(
                "background_wakeup.register_failed run=%s err=%s",
                record.id, type(exc).__name__,
            )
        return record

    async def _schedule_run(
        self,
        record: RunRecord,
        user_input: str,
        *,
        on_event: Callable[[RunEvent], None] | None,
    ) -> RunRecord | None:
        """Persist real host tool receipts on the canonical conversation stream."""
        started = time.monotonic()

        def envelope(kind: str) -> dict[str, Any]:
            result = _plugin_event_envelope(record, kind)
            result["source"] = SourceRef(
                framework="ksadk",
                metadata={"runtime": "studio-scheduler", "agent_id": record.agent_id},
            )
            return result

        async def publish(event: RuntimeEvent) -> None:
            persisted = await self.runtime_events.append_one(record.session_id, event)
            kind, data = project_runtime_event(
                persisted, session_id=record.session_id, public_run_id=record.id
            )
            stored = self.event_store.append(record.id, kind, data)
            if on_event is not None:
                on_event(stored)

        call_id = None
        try:
            intent = await self.schedule_assistant.plan(
                record.agent_id,
                user_input,
                record.session_id,
                build_id=record.build_id,
                model_name=record.model,
            )
            if self._cancel_flags.get(record.id):
                raise asyncio.CancelledError()
            if intent.action == "ignore":
                return None
            record.status = RunStatus.RUNNING
            record.started_at = datetime.now(timezone.utc)
            record.runtime_handle = {"provider": "studio-scheduler"}
            self.event_store.save(record)
            await publish(RunStarted(**envelope("run.started"), status="running"))
            call_id = f"{record.id}:schedule"
            arguments = intent.model_dump(mode="json", exclude_none=True)
            await publish(
                ItemStarted(
                    **envelope("tool.started"),
                    item_id=call_id,
                    item_kind="tool_call",
                    initial=ContentSnapshot(
                        parts=(
                            ToolCallContent(
                                part_id=f"{call_id}:call",
                                call_id=call_id,
                                name="studio_schedule",
                                arguments=arguments,
                            ),
                        )
                    ),
                )
            )
            result = await self.schedule_assistant.execute(
                record.agent_id, record.session_id, intent
            )
            await publish(
                ItemCompleted(
                    **envelope("tool.completed"),
                    item_id=call_id,
                    item_kind="tool_call",
                    snapshot=ContentSnapshot(
                        parts=(
                            ToolResultContent(
                                part_id=f"{call_id}:result", call_id=call_id, result=result
                            ),
                        )
                    ),
                )
            )
            output = result["message"]
            item_id, part_id = f"{record.id}:assistant", f"{record.id}:text"
            await publish(
                ItemStarted(
                    **envelope("item.started"),
                    item_id=item_id,
                    item_kind="message",
                    phase="final_answer",
                )
            )
            await publish(
                ItemUpdated(
                    **envelope("item.updated"),
                    item_id=item_id,
                    item_kind="message",
                    op="append",
                    update=TextContent(part_id=part_id, text=output),
                )
            )
            await publish(
                ItemCompleted(
                    **envelope("item.completed"),
                    item_id=item_id,
                    item_kind="message",
                    snapshot=ContentSnapshot(parts=(TextContent(part_id=part_id, text=output),)),
                )
            )
            if intent._usage is not None and intent._usage.reported:
                record.usage = intent._usage.model_copy(update={"source": "studio-scheduler"})
                await publish(
                    UsageReported(
                        **envelope("usage.reported"),
                        input_tokens=record.usage.input_tokens,
                        output_tokens=record.usage.output_tokens,
                        total_tokens=record.usage.total_tokens,
                        cached_tokens=record.usage.cached_input_tokens,
                        reasoning_tokens=record.usage.reasoning_output_tokens,
                    )
                )
            await publish(
                RunCompleted(
                    **envelope("run.completed"),
                    status="completed",
                    output_refs=(OutputRef(scope_id=record.id, item_id=item_id, part_id=part_id),),
                )
            )
            if not str(output or "").strip():
                # 空交付不得判 COMPLETED（2026-09-18 实测事故）：一次 run 跑了 15 次
                # 模型调用、并行 3 个子 Agent、约 17 分钟，最后以 **COMPLETED +
                # output 长度 0 + 产物 0 个** 收场——用户看到"成功"却拿不到任何东西。
                # 宁可报失败并说清原因，也不要让"什么都没交付"伪装成完成。
                raise StudioError(
                    "EMPTY_DELIVERY",
                    "本轮没有产出任何交付内容（输出为空，且工作区没有新产物），"
                    "已按失败处理而不是成功。请重试或检查该 Agent 的委派/落盘策略。",
                    status_code=502,
                )
            record.output = output
            record.status = RunStatus.COMPLETED
        except asyncio.CancelledError:
            record.status = RunStatus.CANCELLED
            record.completed_at = datetime.now(timezone.utc)
            self._cancel_flags.pop(record.id, None)
            await publish(
                RunCanceled(**envelope("run.canceled"), status="canceled", reason="cancelled")
            )
            self.event_store.save(record)
            raise
        except Exception as error:
            code = str(getattr(error, "code", "SCHEDULE_REQUEST_FAILED"))
            record.status = RunStatus.FAILED
            record.error = {"code": code, "message": str(error)}
            if call_id is not None:
                await publish(
                    ItemFailed(
                        **envelope("tool.failed"),
                        item_id=call_id,
                        item_kind="tool_call",
                        error=ErrorInfo(
                            code=code,
                            message=str(error),
                            source="studio-scheduler",
                            scope_id=record.id,
                        ),
                    )
                )
            await publish(
                RunFailed(
                    **envelope("run.failed"),
                    status="failed",
                    error=ErrorInfo(
                        code=code, message=str(error), source="studio-scheduler", scope_id=record.id
                    ),
                )
            )
        record.completed_at = datetime.now(timezone.utc)
        record.duration_ms = int((time.monotonic() - started) * 1000)
        record.duration_source = "studio"
        self._cancel_flags.pop(record.id, None)
        self.event_store.save(record)
        await self._sync_trace(record)
        return record

    async def _sync_trace(self, record: RunRecord) -> None:
        self.event_store.trace_store.sync(record, await self.events(record.id))

    async def _plugin_run(
        self,
        spec: StudioRunSpec,
        user_input: str,
        *,
        record: RunRecord,
        on_event: Callable[[RunEvent], None] | None,
    ) -> RunRecord:
        """Execute a composed Build without translating it into ADK/LangGraph.

        PluginHost retains the activation under the Studio session id.  Harness
        providers write canonical RuntimeEvents directly; providers that only
        implement the minimum request/result protocol receive a canonical
        envelope here so Studio still has one durable conversation/event model.
        """

        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        record.runtime_handle = {
            "provider": "pluginhost",
            "bundleDigest": str(spec.request_config.get("plugin_bundle_digest") or ""),
            "activationKey": record.session_id,
        }
        self.event_store.save(record)
        projected_writer = _ProjectedRunEventWriter(self.event_store, record.id, on_event)
        rows_before = await self.session_service.get_events(record.session_id)
        after_seq = max((int(row.seq_id or 0) for row in rows_before), default=0)
        published_seq = after_seq

        async def publish(events: list[RuntimeEvent]) -> None:
            nonlocal published_seq
            for runtime_event in events:
                # Harness keeps token-level child output in the canonical
                # session trace for diagnostics.  It is deliberately not part
                # of Studio's public run timeline: the activity projection
                # never consumes these private deltas, and persisting each one
                # rewrites the growing run JSON file.  Long-running delegated
                # research can otherwise turn a few useful activity rows into
                # thousands of O(n²) file writes and make the page appear
                # frozen.  Advance the cursor so final catch-up will not replay
                # an omitted delta.
                if _is_private_harness_delta(runtime_event):
                    published_seq = max(published_seq, int(runtime_event.seq or 0))
                    continue
                event_type, data = project_runtime_event(
                    runtime_event,
                    session_id=record.session_id,
                )
                projected_writer.append(event_type, data)
                published_seq = max(published_seq, int(runtime_event.seq or 0))

        async def tail_live_events() -> None:
            # Kernel worker 会把 harness 的增量事实（TEXT_DELTA/ItemUpdated 等）
            # 在执行期间逐条写入 canonical session store；这里实时转发给 SSE
            # 订阅端，避免整轮结束后一次性爆发。终态事件会自然结束订阅。
            async for event in self.runtime_events.subscribe_run(
                record.session_id,
                record.id,
                after_seq=published_seq,
                poll_interval=0.1,
                timeout=600,
            ):
                try:
                    await publish([event])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "failed to publish live plugin event for run %s", record.id
                    )

        tail_task: asyncio.Task[None] | None = None

        async def stop_tail() -> None:
            nonlocal tail_task
            if tail_task is not None:
                tail_task.cancel()
                await asyncio.gather(tail_task, return_exceptions=True)
                tail_task = None

        try:
            if self.plugin_runtime is None:
                raise StudioError(
                    "PLUGIN_RUNTIME_UNAVAILABLE",
                    "Studio 尚未配置 PluginHost Runtime",
                    status_code=503,
                )
            request_metadata = {
                key: value
                for key, value in {
                    "tool_approval_mode": spec.request_config.get("tool_approval_mode"),
                    "collaboration_mode": spec.request_config.get("collaboration_mode"),
                    "goal_objective": spec.request_config.get("goal_objective"),
                    "reasoning_effort": spec.request_config.get("effort"),
                }.items()
                if value not in {None, ""}
            }
            tail_task = asyncio.create_task(tail_live_events())
            try:
                result = await self.plugin_runtime.execute(
                    spec,
                    {
                        "user_id": "local-user",
                        "session_id": record.session_id,
                        "invocation_id": record.id,
                        "messages": [{"role": "user", "content": user_input}],
                        "model": spec.model,
                        "request_metadata": request_metadata,
                    },
                    session_id=record.session_id,
                )
            finally:
                await stop_tail()
            if result.session_id != record.session_id:
                raise StudioError(
                    "PLUGIN_SESSION_MISMATCH",
                    "AgentProvider 返回了不同的 Session",
                    status_code=502,
                    details={
                        "expected": record.session_id,
                        "actual": result.session_id,
                    },
                )
            canonical = await self._plugin_events_after(
                record.session_id,
                after_seq=published_seq,
                run_id=record.id,
            )
            if not canonical:
                canonical = await self._persist_plugin_result_events(
                    record,
                    result.output_text,
                    usage=result.usage,
                )
            await publish(canonical)
            _require_delivery(result.output_text)
            record.output = result.output_text
            record.status = RunStatus.COMPLETED
            _apply_plugin_usage(record, result.usage)
        except asyncio.CancelledError:
            await stop_tail()
            record.status = RunStatus.CANCELLED
            record.error = {"code": "RUN_CANCELLED", "message": "运行已取消"}
            cancelled = await self.runtime_events.append_one(
                record.session_id,
                RunCanceled(
                    **_plugin_event_envelope(record, "run.canceled"),
                    status="canceled",
                    reason="cancelled",
                ),
            )
            await publish([cancelled])
            raise
        except Exception as exc:  # noqa: BLE001 - plugin boundary is typed below
            await stop_tail()
            record.status = RunStatus.FAILED
            code = str(getattr(exc, "code", "PLUGIN_RUNTIME_FAILED"))
            record.error = {"code": code, "message": str(exc)}
            canonical = await self._plugin_events_after(
                record.session_id,
                after_seq=published_seq,
                run_id=record.id,
            )
            if canonical:
                await publish(canonical)
            else:
                failed = await self.runtime_events.append_one(
                    record.session_id,
                    RunFailed(
                        **_plugin_event_envelope(record, "run.failed"),
                        status="failed",
                        error=ErrorInfo(
                            code=code,
                            message=str(exc),
                            source="pluginhost",
                            scope_id=record.id,
                        ),
                    ),
                )
                await publish([failed])
        finally:
            projected_writer.flush()
            record.completed_at = datetime.now(timezone.utc)
            record.duration_ms = int((time.monotonic() - started) * 1000)
            record.duration_source = "studio"
            self.event_store.save(record)
            await self._sync_trace(record)
        return record

    async def _plugin_events_after(
        self,
        session_id: str,
        *,
        after_seq: int,
        run_id: str,
    ) -> list[RuntimeEvent]:
        rows = await self.session_service.get_events(
            session_id,
            after_seq_id=after_seq,
        )
        events: list[RuntimeEvent] = []
        for row in rows:
            event = session_event_to_runtime_event(row)
            if event is not None and event.run_id == run_id:
                events.append(event)
        return events

    async def _persist_plugin_result_events(
        self,
        record: RunRecord,
        output_text: str,
        *,
        usage: Mapping[str, Any],
    ) -> list[RuntimeEvent]:
        item_id = f"{record.id}:assistant"
        part_id = f"{record.id}:text"
        snapshot = ContentSnapshot(parts=(TextContent(part_id=part_id, text=output_text),))
        events: list[RuntimeEvent] = [
            RunStarted(
                **_plugin_event_envelope(record, "run.started"),
                status="running",
            ),
            ItemStarted(
                **_plugin_event_envelope(record, "item.started"),
                item_id=item_id,
                item_kind="message",
                phase="final_answer",
            ),
            ItemUpdated(
                **_plugin_event_envelope(record, "item.updated"),
                item_id=item_id,
                item_kind="message",
                op="append",
                update=TextContent(part_id=part_id, text=output_text),
            ),
            ItemCompleted(
                **_plugin_event_envelope(record, "item.completed"),
                item_id=item_id,
                item_kind="message",
                snapshot=snapshot,
            ),
        ]
        normalized = _normalized_plugin_usage(usage)
        if normalized["reported"]:
            events.append(
                UsageReported(
                    **_plugin_event_envelope(record, "usage.reported"),
                    input_tokens=normalized["input_tokens"],
                    output_tokens=normalized["output_tokens"],
                    total_tokens=normalized["total_tokens"],
                    cached_tokens=normalized["cached_tokens"],
                    reasoning_tokens=normalized["reasoning_tokens"],
                )
            )
        events.append(
            RunCompleted(
                **_plugin_event_envelope(record, "run.completed"),
                status="completed",
                output_refs=(
                    OutputRef(
                        scope_id=record.id,
                        item_id=item_id,
                        part_id=part_id,
                    ),
                ),
            )
        )
        return [await self.runtime_events.append_one(record.session_id, event) for event in events]

    async def _kernel_run(
        self,
        spec: StudioRunSpec,
        user_input: str,
        *,
        record: RunRecord,
        on_event: Callable[[RunEvent], None] | None,
        kernel_runtime: Any,
    ) -> RunRecord:
        """kernel 路径（灰度 opt-in）：Studio run -> AgentControlCommand -> receipt。

        mutation 只走 kernel.submit；Studio RunEvent shape 保留，cursor 源自
        同一 Session seq（SessionEventSubscription.after_seq）。
        """
        from ksadk.kernel import ingress as _kernel_ingress

        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        self.event_store.save(record)
        try:
            trusted = _kernel_ingress.trusted_context(
                source_kind="studio",
                source_ref=record.id,
                # The Worker polls the concrete Kernel Runtime instance, not
                # whichever synthetic ``local-agent`` happened to be inferred
                # from a Studio request.  A Studio Build may use the Kernel
                # path only after ``_kernel_runtime_for_spec`` established that
                # it is this Runtime's exact Agent/Runtime binding.
                tenant_id=str(kernel_runtime.config.tenant_id),
                agent_instance_id=str(kernel_runtime.config.agent_instance_id),
                session_id=record.session_id,
                operations=("enqueue",),
            )
            idempotency_key = str((spec.request_config or {}).get("idempotency_key") or record.id)
            command = _kernel_ingress.map_studio_request(
                session_id=record.session_id,
                idempotency_key=idempotency_key,
                content=user_input,
                run_id=record.id,
                trusted=trusted,
            )
            receipt = await _kernel_ingress.submit_command(command, permit=trusted.permit)
            if receipt.status not in ("accepted", "duplicate"):
                record.status = RunStatus.FAILED
                record.error = {
                    "code": "kernel_command_rejected",
                    "message": f"agent kernel rejected command: {receipt.status}",
                }
                self.event_store.save(record)
                return record
            output_text = ""
            async for _seq, projected in _kernel_ingress.subscribe_projected(
                record.session_id,
                trusted=trusted,
                after_seq=int(receipt.accepted_seq or 0),
                projector=_studio_envelope_projection,
            ):
                if projected is None:
                    continue
                event_type, data = projected
                if event_type == "message.delta":
                    # Canonical RuntimeEvent projection names the visible text
                    # field ``text``; legacy Studio envelopes used ``delta``.
                    # The Kernel path consumes both because it may replay a
                    # mixed-version session during a rolling upgrade.
                    output_text += str(data.get("text") or data.get("delta") or "")
                elif event_type == "run.completed":
                    output_text = str(data.get("output_text") or output_text)
                stored = self.event_store.append(record.id, event_type, data)
                if on_event is not None:
                    on_event(stored)
            _require_delivery(output_text)
            record.output = output_text
            record.status = RunStatus.COMPLETED
            record.completed_at = datetime.now(timezone.utc)
            record.duration_ms = int((time.monotonic() - started) * 1000)
            self.event_store.save(record)
            await self._sync_trace(record)
            return record

        except Exception as exc:  # noqa: BLE001
            logger.exception("Studio kernel ingress failed for run %s", record.id)
            record.status = RunStatus.FAILED
            record.error = {
                "code": "kernel_ingress_failed",
                "message": str(exc),
            }
            record.completed_at = datetime.now(timezone.utc)
            record.duration_ms = int((time.monotonic() - started) * 1000)
            self.event_store.save(record)
            await self._sync_trace(record)
            return record

    @staticmethod
    def _kernel_runtime_for_spec(spec: StudioRunSpec) -> Any | None:
        """Return the active Kernel Runtime only for its bound Studio Build.

        An in-process AgentKernel owns one concrete RuntimeAdapter factory and
        its immutable startup defaults.  Merely observing that a Kernel is
        enabled is therefore insufficient: sending an arbitrary Studio Build
        through it can silently execute the default Adapter instead.  When the
        binding is not exact, the existing direct Studio path remains the
        compatible local behaviour; it is safer than manufacturing a false
        AgentControl/Scheduler success.
        """

        from ksadk.kernel.bootstrap import get_agent_kernel_runtime
        from ksadk.kernel.ingress import kernel_route_active

        if not kernel_route_active():
            return None
        runtime = get_agent_kernel_runtime()
        if runtime is None:
            return None
        active_context = getattr(runtime.config, "launch_context", None)
        if active_context is None:
            return None
        if str(getattr(active_context, "runtime_type", "")).strip().lower() != (
            spec.launch_context.runtime_type.strip().lower()
        ):
            return None
        try:
            active_project = Path(active_context.project_dir).resolve()
            build_project = Path(spec.launch_context.project_dir).resolve()
        except (OSError, TypeError, ValueError):
            return None
        if active_project != build_project:
            return None
        defaults = getattr(runtime.config, "start_request_defaults", {}) or {}
        bound_agent_id = str(defaults.get("agent_id") or runtime.config.agent_instance_id).strip()
        if bound_agent_id != spec.agent_id:
            return None
        declared_instance = str(
            (spec.launch_context.config or {}).get("agent_instance_id") or ""
        ).strip()
        if declared_instance and declared_instance != str(runtime.config.agent_instance_id):
            return None
        return runtime

    async def cancel_run(self, run_id: str) -> dict[str, str]:
        """Request cancellation; the flag and executor perform the actual stop."""
        self._cancel_flags[run_id] = True
        recovery_ready = self._native_recovery_ready.pop(run_id, None)
        if recovery_ready is not None:
            recovery_ready.set()
        queue = self._control_queues.get(run_id)
        if queue is not None:
            queue.put_nowait(("cancel", None, None))
        handle = self._active_handles.get(run_id)
        if handle is not None and self.executor.is_attached(handle):
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.executor.cancel(handle)),
                    timeout=_CANCEL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # The cancellation flag wakes the Studio state machine; final
                # transport teardown remains close()'s responsibility.
                pass
            except Exception:
                pass
        return {"runId": run_id, "status": "cancelling"}









    async def resume_run(self, run_id: str) -> dict[str, str]:
        record = self.event_store.get(run_id)
        if record.status != RunStatus.PAUSED:
            raise StudioError("RUN_NOT_PAUSED", "只有已暂停的 Run 可以继续", status_code=409)
        queue = self._control_queues.get(run_id)
        if queue is None:
            raise StudioError(
                "RUN_RESUME_UNAVAILABLE",
                "Studio 进程已重启，当前暂停点无法恢复",
                status_code=409,
            )
        # A paused-run continuation is a control action, with no new user
        # message or approval. Keep the RuntimeAdapter's empty-payload seam.
        queue.put_nowait(("resume", None, None))
        return {"runId": run_id, "status": "resuming"}

    async def submit_interaction(
        self,
        run_id: str,
        interaction_id: str,
        *,
        name: str,
        data: dict[str, Any] | None = None,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        lock = self._interaction_locks.setdefault(run_id, asyncio.Lock())
        payload_data = dict(data or {})
        digest = hashlib.sha256(
            json.dumps(
                {"name": name, "data": payload_data},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        async with lock:
            return await self._submit_interaction_locked(
                run_id,
                interaction_id,
                name=name,
                data=payload_data,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                request_digest=digest,
            )

    async def _submit_interaction_locked(
        self,
        run_id: str,
        interaction_id: str,
        *,
        name: str,
        data: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any]:
        from ksadk.studio.interaction_submission import submit_interaction_locked

        return await submit_interaction_locked(
            self, run_id, interaction_id, name=name, data=data,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
            request_digest=request_digest, revision_of=_interaction_revision,
            submit_timeout_seconds=_INTERACTION_SUBMIT_TIMEOUT_SECONDS,
        )

    def _expire_unresolved_interactions(
        self,
        record: RunRecord,
        *,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> None:
        """Close orphaned cards whenever their owning run is terminal."""

        if record.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
            RunStatus.TIMED_OUT,
        }:
            return
        events = self.event_store.events(record.id)
        closed_ids = {
            str(event.data.get("interactionId") or event.data.get("approvalId") or "")
            for event in events
            if event.type
            in {
                "a2ui.action",
                "approval.resolved",
                "interaction.resolved",
                "interaction.cancelled",
                "interaction.expired",
            }
        }
        pending: dict[str, RunEvent] = {}
        for event in events:
            if event.type != "a2ui.interaction":
                continue
            interaction_id = str(
                event.data.get("interactionId") or event.data.get("interaction_id") or ""
            )
            if interaction_id and interaction_id not in closed_ids:
                pending[interaction_id] = event
        for interaction_id, requested in pending.items():
            data = requested.data
            expired = self.event_store.append(
                record.id,
                "interaction.expired",
                {
                    "runId": record.id,
                    "interactionId": interaction_id,
                    "approvalId": interaction_id,
                    "callId": str(data.get("callId") or data.get("call_id") or ""),
                    "kind": str(data.get("kind") or "form"),
                    "title": data.get("title"),
                    "message": data.get("message"),
                    "detail": data.get("detail"),
                    "outcome": "expired",
                    "revision": _interaction_revision(data) + 1,
                },
            )
            if on_event is not None:
                on_event(expired)

    async def _capture_pcm_evidence(
        self,
        record: RunRecord,
        spec: StudioRunSpec,
        user_input: str,
    ) -> Any | None:
        """Prepare once and persist the exact prompt/context input sent to Runtime."""

        try:
            from ksadk.conversations.runtime_preparation import build_run_input
            from ksadk.sessions.in_memory import InMemorySessionService

            temporary_sessions = InMemorySessionService()
            await temporary_sessions.create_session(
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=record.session_id,
            )
            config = dict(spec.request_config or {})
            prepared = await build_run_input(
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=record.session_id,
                messages=self._conversation_messages(
                    spec.agent_id,
                    record.session_id,
                    user_input,
                ),
                model=spec.model,
                instructions=str(config.get("instructions") or ""),
                agent_system=str(config.get("agent_system") or ""),
                agent_task=str(config.get("agent_task") or ""),
                prompt_integration_mode=str(config.get("prompt_integration_mode") or ""),
                context_engine_rollout=str(config.get("context_engine_rollout") or "") or None,
                memory_recall_enabled=config.get("memory_recall_enabled"),
                memory_write_rollout=str(config.get("memory_write_rollout") or "") or None,
                memory_enabled=config.get("memory_enabled"),
                memory_write_mode=str(config.get("memory_write_mode") or "candidate"),
                flush_before_compaction=bool(config.get("flush_before_compaction", True)),
                provider_ref=str(config.get("provider_ref") or "local-default"),
                runtime_type=spec.launch_context.runtime_type,
                deployment_mode=spec.launch_context.deployment_mode,
                invocation_id=record.id,
                session_service_provider=lambda: temporary_sessions,
                agent_max_input_tokens=config.get("max_input_tokens"),
                agent_reserve_output_tokens=config.get("reserve_output_tokens"),
            )
            record.context_plan = prepared.context_plan
            compiled = prepared.compiled_prompt
            shadow = prepared.shadow_context_plan or {}
            record.prompt_evidence = {
                "contentHash": (compiled or {}).get("prompt_content_hash"),
                "stablePrefixHash": (compiled or {}).get("prompt_stable_prefix_hash"),
                "sectionHashes": (compiled or {}).get("prompt_section_hashes", {}),
                "tokensBySection": (compiled or {}).get("prompt_tokens_by_section", {}),
                "estimatedTokens": (compiled or {}).get("prompt_estimated_tokens"),
                "sectionCount": (compiled or {}).get("prompt_section_count"),
                "integrationMode": shadow.get("integration_mode")
                or config.get("prompt_integration_mode")
                or "native",
                "accountingAccuracy": shadow.get("accounting_accuracy") or "opaque",
                "promptOwner": shadow.get("prompt_owner") or "runtime",
                "runtimeType": shadow.get("runtime_type") or spec.launch_context.runtime_type,
                "deploymentMode": shadow.get("deployment_mode")
                or spec.launch_context.deployment_mode,
                "capabilityHash": shadow.get("capability_hash"),
                "tokensByKind": shadow.get("tokens_by_kind", {}),
                "plannedInputTokens": shadow.get("planned_input_tokens"),
            }
            record.working_state = prepared.working_state
            for event in getattr(prepared, "memory_recall_events", []):
                self.event_store.append(
                    record.id,
                    event.get("type", "memory.recall.event"),
                    event,
                )
            self.event_store.save(record)
            return prepared
        except Exception:  # noqa: BLE001 - evidence collection is best effort
            logger.exception("Studio PCM evidence capture failed for run %s", record.id)
            return None

    def _persist_approval_surface(
        self,
        record: RunRecord,
        event: InteractionRequested,
        *,
        on_event: Callable[[RunEvent], None] | None,
    ) -> None:
        approval_id = event.interaction_id
        if not approval_id:
            return
        surface_id = f"approval-{approval_id}"
        detail_value = event.request.detail
        detail = detail_value if isinstance(detail_value, dict) else {}
        command = str(detail.get("command") or detail.get("reason") or "")
        kind = str(event.request.kind or "tool")
        components = [
            {
                "id": "root",
                "component": "Card",
                "title": "需要你的确认",
                "children": ["approval"],
            },
            {
                "id": "approval",
                "component": "ApprovalBar",
                "tool_name": kind,
                "summary": command or f"Agent 请求执行 {kind} 操作",
                "approve_label": "批准",
                "deny_label": "拒绝",
            },
        ]
        begin = self.event_store.append(
            record.id,
            "a2ui.surface.begin",
            {
                "runId": record.id,
                "surfaceId": surface_id,
                "a2uiOperations": [
                    {
                        "version": "v0.9",
                        "createSurface": {
                            "surfaceId": surface_id,
                            "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
                        },
                    },
                    {
                        "version": "v0.9",
                        "updateComponents": {
                            "surfaceId": surface_id,
                            "components": components,
                        },
                    },
                ],
            },
        )
        interaction = self.event_store.append(
            record.id,
            "a2ui.interaction",
            {
                "runId": record.id,
                "surfaceId": surface_id,
                "interactionId": approval_id,
                "callId": event.request.call_id or approval_id,
                "kind": "approval",
                "title": "需要审批",
                "message": "此次操作需要确认后才能继续。",
                "detail": detail,
                "revision": 1,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "decision": {
                            "type": "string",
                            "enum": ["approve", "approve_session", "deny"],
                        }
                    },
                    "required": ["decision"],
                },
            },
        )
        if on_event is not None:
            on_event(begin)
            on_event(interaction)

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
            assistant_content = previous.output
            if previous.runtime_type == "harness":
                projection = project_run_activities(
                    previous,
                    self.event_store.events(previous.id),
                )
                children = [
                    activity
                    for activity in projection.get("activities", [])
                    if activity.get("kind") == "subagent"
                ][:8]
                if children:
                    status_text = {
                        "completed": "已完成",
                        "failed": "失败",
                        "cancelled": "已取消",
                        "running": "仍在运行",
                        "unknown": "状态未确认",
                    }
                    facts = "；".join(
                        f"子智能体“{child.get('label') or '未命名'}”"
                        f"{status_text.get(str(child.get('status')), '状态未确认')}"
                        for child in children
                    )
                    # Lifecycle evidence belongs to the completed assistant turn.
                    # A standalone system message inserted between historical turns
                    # can outweigh the next user request on some providers and make
                    # a new turn answer an old child result instead (conversation
                    # "cross-talk"). Keep the evidence attached to the turn it
                    # describes so the newest user message remains authoritative.
                    assistant_content = (
                        f"{assistant_content}\n\n"
                        f"[Harness 执行记录] {facts}。"
                    )
            messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": user_input})
        return messages

    def _native_session_metadata(
        self,
        agent_id: str,
        session_id: str,
        runtime_type: str,
        build_id: str,
    ) -> dict[str, str]:
        if runtime_type != "codex":
            return {}
        runs = self.event_store.list_runs(session_id=session_id, agent_id=agent_id)
        thread_builds: dict[str, str] = {}
        for previous in runs:
            native_ref = previous.runtime_handle.get("native_ref") or {}
            if isinstance(native_ref, dict) and native_ref.get("thread_id"):
                # Older Studio versions also saved failed cross-build resumes.
                # The first occurrence identifies the thread's actual home.
                thread_builds.setdefault(str(native_ref["thread_id"]), previous.build_id)
        for previous in reversed(runs):
            # Each immutable build has its own CODEX_HOME and plugin snapshot.
            # Starting a thread in that home applies the new capabilities; the
            # transport-neutral history above preserves the Studio conversation.
            if previous.build_id != build_id:
                return {}
            native_ref = previous.runtime_handle.get("native_ref")
            native_ref = native_ref if isinstance(native_ref, dict) else {}
            thread_id = str(native_ref.get("thread_id") or "")
            if thread_id:
                if thread_builds[thread_id] != build_id:
                    return {}
                return {"thread_id": thread_id}
        return {}


def _plugin_event_envelope(record: RunRecord, event_type: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "event_id": f"{record.id}:{event_type}",
        "seq": 0,
        "timestamp": time.time(),
        "run_id": record.id,
        "scope_id": record.id,
        "source": SourceRef(
            framework="ksadk",
            metadata={
                "runtime": "pluginhost",
                "agent_id": record.agent_id,
                "session_id": record.session_id,
                "build_id": record.build_id,
            },
        ),
    }


def _normalized_plugin_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    def number(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0
        return 0

    normalized = {
        "input_tokens": number("input_tokens", "inputTokens", "prompt_tokens"),
        "output_tokens": number("output_tokens", "outputTokens", "completion_tokens"),
        "total_tokens": number("total_tokens", "totalTokens"),
        "cached_tokens": number("cached_tokens", "cachedTokens"),
        "reasoning_tokens": number("reasoning_tokens", "reasoningTokens"),
    }
    if not normalized["total_tokens"]:
        normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    normalized["reported"] = bool(usage) and any(
        key in usage
        for key in (
            "input_tokens",
            "inputTokens",
            "prompt_tokens",
            "output_tokens",
            "outputTokens",
            "completion_tokens",
            "total_tokens",
            "totalTokens",
        )
    )
    return normalized


def _interaction_revision(data: Mapping[str, Any]) -> int:
    """Treat malformed or historical interaction revisions as read-only."""

    try:
        return max(0, int(data.get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def _apply_usage_event(
    record: RunRecord, event: UsageReported, runtime_type: str = ""
) -> None:
    """Apply a canonical UsageReported event to the run record by ``mode``.

    长任务方案 §19.2 usage_mode / 交接记录 §6 Usage 计量缺陷修复：
    - ``delta``：单次调用的增量，累加到 record.usage（引擎每模型调用一条）；
    - ``cumulative`` / ``snapshot``：provider 已给出累计/快照值，直接覆盖。
    缺省按 ``delta`` 处理，保持旧事件「逐次累加」语义不回退。
    """
    mode = getattr(event, "mode", "delta") or "delta"
    source = str(event.source.framework or runtime_type or "")
    if mode == "delta":
        record.usage = Usage(
            input_tokens=record.usage.input_tokens + event.input_tokens,
            output_tokens=record.usage.output_tokens + event.output_tokens,
            total_tokens=record.usage.total_tokens + event.total_tokens,
            cached_input_tokens=record.usage.cached_input_tokens + event.cached_tokens,
            reasoning_output_tokens=record.usage.reasoning_output_tokens + event.reasoning_tokens,
            reported=True,
            source=source or record.usage.source,
        )
    else:
        record.usage = Usage(
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            total_tokens=event.total_tokens,
            cached_input_tokens=event.cached_tokens,
            reasoning_output_tokens=event.reasoning_tokens,
            reported=True,
            source=source or record.usage.source,
        )


def _apply_plugin_usage(record: RunRecord, usage: Mapping[str, Any]) -> None:
    normalized = _normalized_plugin_usage(usage)
    if not normalized["reported"]:
        return
    record.usage = Usage(
        input_tokens=normalized["input_tokens"],
        output_tokens=normalized["output_tokens"],
        total_tokens=normalized["total_tokens"],
        cached_input_tokens=normalized["cached_tokens"],
        reasoning_output_tokens=normalized["reasoning_tokens"],
        reported=True,
        source="pluginhost",
    )


def _is_private_harness_delta(event: RuntimeEvent) -> bool:
    """Return whether *event* is an internal token delta, not a UI activity.

    Managed Harness projects its rich event stream to canonical status items.
    ``text.delta`` and ``reasoning.delta`` contain private token fragments,
    are intentionally ignored by ``run_activity``, and must not be copied into
    Studio's durable session or run stores. User-visible parent answer
    streaming uses canonical ``message`` items and therefore remains
    unaffected.
    """

    if not isinstance(event, ItemCompleted) or event.item_kind != "status":
        return False
    if event.source.framework != "ksadk":
        return False
    native_event_type = str(event.source.metadata.get("native_event_type") or "")
    return native_event_type in {"reasoning.delta", "text.delta"}


def _is_pause_interruption(event: RunInterrupted) -> bool:
    return event.reason == "user_pause" or (
        event.reason == "paused" and event.source.metadata.get("engine_kind") == "native"
    )


def project_runtime_event(
    event: RuntimeEvent,
    *,
    session_id: str | None = None,
    public_run_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Project the canonical RuntimeEvent into Studio's persisted event view.

    公开承诺字段（契约声明见 ``ksadk/events/projections.py``，执行形态为
    ``tests/protocol/test_cross_projection_golden.py``）：
    - 所有事件 payload 必含 ``runId``/``scopeId``；
    - ``message.*``/``thinking.*`` 含 ``itemId``（delta 另含 ``partId``）；
    - ``tool.*``/``command.*``/``approval.*``/``a2ui.*`` 含 ``itemId``；
    - ``a2ui.surface.*`` 含 ``surfaceId`` 与 ``a2uiOperations`` 列表。

    内部不保证字段：除上述外的 payload 附加键、事件类型枚举的完备性
    （新增 canonical 事件类型在未适配前可能整条丢弃）。
    """

    if isinstance(event, RunStarted):
        projected = "run.started"
        payload: dict[str, Any] = {}
    elif isinstance(event, RunProgress):
        projected = "run.progress"
        payload = {"progress": event.progress, "message": event.message}
    elif isinstance(event, RunCompleted):
        projected = "run.completed"
        payload = {}
    elif isinstance(event, RunFailed):
        projected = "run.failed"
        payload = {"error": event.error.message or ""}
    elif isinstance(event, RunCanceled):
        projected = "run.cancelled"
        payload = {"reason": event.reason or ""}
    elif isinstance(event, RunInterrupted):
        projected = "run.paused" if _is_pause_interruption(event) else "run.interrupted"
        payload = {"reason": event.reason or ""}
    elif isinstance(event, ItemStarted) and event.item_kind in {"message", "reasoning"}:
        # 首段文本随 ItemStarted 到达；不投影会让 live 流丢掉第一个 chunk
        # （增量在 ItemUpdated 里才开始出现）。
        text = ""
        if event.initial:
            for part in event.initial.parts:
                if isinstance(part, TextContent):
                    text += part.text
        is_thinking = event.item_kind == "reasoning"
        if text:
            projected = "thinking.delta" if is_thinking else "message.delta"
            payload = {"text": text}
        else:
            projected = "thinking.started" if is_thinking else "message.started"
            payload = {"itemId": event.item_id}
    elif isinstance(event, ItemUpdated) and event.item_kind in {"message", "reasoning"}:
        text = event.update.text if isinstance(event.update, TextContent) else ""
        is_thinking = event.item_kind == "reasoning"
        projected = "thinking.delta" if is_thinking else "message.delta"
        payload = {"text": text}
    elif isinstance(event, ItemCompleted) and event.item_kind in {"message", "reasoning"}:
        text = ""
        for part in event.snapshot.parts:
            if isinstance(part, TextContent):
                text = part.text
                break
        is_thinking = event.item_kind == "reasoning"
        projected = "thinking.completed" if is_thinking else "message.completed"
        payload = {"text": text}
    elif isinstance(event, ItemStarted) and event.item_kind == "tool_call":
        tool_part = _first_content(event.initial, ToolCallContent) if event.initial else None
        if tool_part is not None and tool_part.name == "codex.command":
            projected = "command.started"
            args = tool_part.arguments if isinstance(tool_part.arguments, dict) else {}
            payload = {
                "callId": tool_part.call_id,
                "command": str(args.get("command") or ""),
                "cwd": str(args.get("cwd") or ""),
                "commandActions": args.get("command_actions") or [],
            }
        elif tool_part is not None:
            projected = "tool.started"
            payload = {
                "callId": tool_part.call_id,
                "tool": tool_part.name,
                "args": tool_part.arguments,
            }
        else:
            projected = "tool.started"
            payload = {}
    elif (isinstance(event, ItemUpdated) and event.item_kind == "tool_call"
          and event.source.metadata.get("tool_display")):
        projected = "tool.progress"
        payload = {"callId": event.source.metadata.get("call_id"),
                   "tool": event.source.metadata.get("tool_name"), "status": "running"}
    elif isinstance(event, ItemCompleted) and event.item_kind == "tool_call":
        tool_call = _first_content(event.snapshot, ToolCallContent)
        tool_result = _first_content(event.snapshot, ToolResultContent)
        if tool_call is not None and tool_call.name == "codex.command":
            projected = "command.completed"
            result = (
                tool_result.result
                if tool_result is not None and isinstance(tool_result.result, dict)
                else {}
            )
            payload = {
                "callId": tool_call.call_id,
                "status": (
                    "failed" if tool_result is not None and tool_result.is_error
                    else str(result.get("status") or "")
                ),
                "exitCode": result.get("exit_code"),
                "durationMs": result.get("duration_ms"),
                "output": str(result.get("output") or ""),
            }
        else:
            projected = "tool.completed"
            result = (
                tool_result.result
                if tool_result is not None and isinstance(tool_result.result, dict)
                else {}
            )
            tool_status = str(result.get("status") or "")
            if tool_result is not None and tool_result.is_error:
                tool_status = "failed"
            call_id = (tool_call or tool_result).call_id if (tool_call or tool_result) else ""
            payload = {
                "callId": call_id,
                "tool": tool_call.name if tool_call is not None else "",
                "status": tool_status,
                "durationMs": result.get("duration_ms"),
                "output": str(result.get("output") or ""),
            }
            if tool_result is not None and tool_result.is_error:
                payload["error"] = str(result.get("error") or "")
    elif (
        isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted))
        and event.item_kind == "data"
        and event.source.protocol == "a2ui"
    ):
        projected, payload = _project_a2ui_surface(event)
    elif isinstance(event, InteractionRequested):
        if event.interaction_kind == "approval":
            projected = "approval.requested"
            payload = {
                "approvalId": event.interaction_id,
                "callId": event.request.call_id or "",
                "kind": event.request.kind,
                "detail": event.request.detail,
            }
        else:
            projected = "a2ui.interaction"
            payload = {
                "interactionId": event.interaction_id,
                "kind": "form",
                "inputSchema": event.request.schema_,
                "message": event.request.prompt or "",
                "callId": event.source.native_event_id or event.interaction_id,
            }
    elif isinstance(event, InteractionResolved):
        if event.interaction_kind == "approval":
            projected = "approval.resolved"
            payload = {
                "approvalId": event.interaction_id,
                "callId": str(event.source.metadata.get("call_id") or ""),
                "decision": event.response.decision,
            }
        else:
            projected = "a2ui.action"
            payload = {"interactionId": event.interaction_id}
    elif isinstance(event, ContinuationCreated):
        projected = "checkpoint.created"
        payload = {
            "checkpointId": event.continuation_id,
            "granularity": event.ref.get("granularity", "snapshot"),
            "resumable": event.resumable,
        }
    elif isinstance(event, ContinuationResumed):
        projected = "checkpoint.resumed"
        payload = {
            "checkpointId": event.continuation_id,
            "resumeAttemptId": event.resume_attempt_id,
        }
    elif isinstance(event, UsageReported):
        projected = "usage.reported"
        payload = {
            "inputTokens": event.input_tokens,
            "outputTokens": event.output_tokens,
            "totalTokens": event.total_tokens,
            "cachedTokens": event.cached_tokens,
            "reasoningTokens": event.reasoning_tokens,
        }
    else:
        projected = event.event_type
        payload = {}

    _attach_studio_identity(payload, event)
    if public_run_id and public_run_id != event.run_id:
        payload["runtimeRunId"] = event.run_id
        payload["runId"] = public_run_id
    payload["runtimeEvent"] = dump_runtime_event(event)
    # Additive only: legacy Studio/Web consumers keep reading the established
    # event type and payload keys.  New surfaces may opt into this typed,
    # identity-aware representation without reconstructing one from text.
    payload["conversationItem"] = project_conversation_item(
        event,
        session_id=session_id,
        run_id=public_run_id,
    ).model_dump(by_alias=True, exclude_none=True, mode="json")
    return projected, payload


def _first_content(snapshot: ContentSnapshot | None, content_type: type) -> Any | None:
    if snapshot is None:
        return None
    for part in snapshot.parts:
        if isinstance(part, content_type):
            return part
    return None


def _project_a2ui_surface(
    event: ItemStarted | ItemUpdated | ItemCompleted,
) -> tuple[str, dict[str, Any]]:
    if isinstance(event, ItemStarted):
        projected = "a2ui.surface.begin"
        data_parts = event.initial.parts if event.initial is not None else ()
    elif isinstance(event, ItemUpdated):
        projected = "a2ui.surface.update"
        data_parts = (event.update,) if isinstance(event.update, DataContent) else ()
    else:
        projected = "a2ui.surface.end"
        data_parts = event.snapshot.parts

    if event.source.metadata.get("operation_batch") is True and isinstance(event, ItemCompleted):
        # Completion closes the immutable canonical batch; it is not a request
        # to remove the surface that the batch just created.
        projected = "a2ui.surface.begin"

    surface_id = str(event.source.metadata.get("surface_id") or "")
    operations: list[dict[str, Any]] = []
    for part in data_parts:
        if isinstance(part, DataContent):
            data = part.data
            if isinstance(data, list):
                operations.extend(dict(op) for op in data if isinstance(op, Mapping))
            elif isinstance(data, Mapping):
                operations.extend(project_a2ui_operations(projected, data))
    if not operations:
        operations = project_a2ui_operations(projected, {"surface_id": surface_id})
    payload: dict[str, Any] = {
        "surfaceId": surface_id,
        "a2uiOperations": operations,
    }
    return projected, payload


def _attach_studio_identity(payload: dict[str, Any], event: RuntimeEvent) -> None:
    """Attach §8.4 identity fields (runId/scopeId/itemId/partId/operation)."""
    payload["runId"] = event.run_id
    payload["scopeId"] = event.scope_id
    if isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted, ItemFailed)):
        payload["itemId"] = event.item_id
    if isinstance(event, ItemUpdated):
        payload["operation"] = event.op
    if isinstance(event, ItemUpdated) and hasattr(event.update, "part_id"):
        payload["partId"] = event.update.part_id
    if isinstance(event, ItemStarted) and event.initial is not None:
        for part in event.initial.parts:
            if hasattr(part, "part_id"):
                payload["partId"] = part.part_id
                break
    if isinstance(event, ItemCompleted):
        for part in event.snapshot.parts:
            if hasattr(part, "part_id"):
                payload["partId"] = part.part_id
                break
    if isinstance(event, (InteractionRequested, InteractionResolved)):
        payload["itemId"] = event.interaction_id
    if isinstance(event, (ContinuationCreated, ContinuationResumed)):
        payload["itemId"] = event.continuation_id


__all__ = ["StudioRunService", "StudioRunSpec", "project_runtime_event"]


def _studio_envelope_projection(envelope) -> tuple[str, dict[str, Any]] | None:
    """Session envelope -> Studio RunEvent 投影；cursor 仍用 envelope.seq。"""

    payload = envelope.payload or {}
    # Kernel RuntimeEvent is persisted as a typed runtime/v2 SessionEvent
    # envelope.  Reuse the exact same projector as the direct Studio path so
    # a consumer receives its additive ConversationItem regardless of which
    # ingress delivered the Run.  Legacy event families retain the historical
    # text-only fallback below.
    if envelope.family == "runtime" and envelope.family_version == 2:
        try:
            runtime_event = parse_runtime_event(payload)
        except (TypeError, ValueError):
            # A damaged/unknown runtime payload must not make old Studio
            # subscriptions fail.  Preserve its event type for legacy
            # observability while refusing to invent a typed conversation item.
            return envelope.event_type, {"runId": envelope.run_id or ""}
        return project_runtime_event(runtime_event, session_id=envelope.session_id)
    if envelope.family == "interaction" and envelope.family_version == 1:
        conversation_item = project_interaction_conversation_item(envelope)
        if conversation_item is None:
            # Keep a malformed durable fact observable without reusing an
            # actionable legacy event name.  Existing Studio reducers turn
            # ``approval.requested`` into a submit button, so emitting that
            # name without an authoritative revision would bypass the typed
            # ConversationItem fail-closed boundary.
            payload = envelope.payload or {}
            return envelope.event_type, {
                "runId": envelope.run_id or str(payload.get("run_id") or ""),
                "itemId": str(payload.get("interaction_id") or envelope.event_id),
                "interactionReadOnly": True,
            }
        projected = _project_interaction_envelope_legacy(envelope)
        if projected is None:
            return None
        event_type, data = projected
        data["conversationItem"] = conversation_item.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        return event_type, data
    if envelope.event_type == "run.completed":
        return "run.completed", {
            "runId": envelope.run_id or "",
            "output_text": str(payload.get("output_text") or ""),
        }
    text = str(payload.get("delta") or payload.get("text") or "")
    if text:
        return "message.delta", {"delta": text}
    return None


def _project_interaction_envelope_legacy(
    envelope,
) -> tuple[str, dict[str, Any]] | None:
    """Keep established Studio event names while Interaction/v1 owns truth."""

    payload = envelope.payload or {}
    interaction_id = str(payload.get("interaction_id") or "")
    interaction_kind = str(payload.get("kind") or "")
    revision = payload.get("revision")
    request = payload.get("request")
    request = request if isinstance(request, Mapping) else {}
    presentation = request.get("presentation")
    presentation = presentation if isinstance(presentation, Mapping) else {}
    common: dict[str, Any] = {
        "runId": envelope.run_id or str(payload.get("run_id") or ""),
        "itemId": interaction_id,
        "interactionId": interaction_id,
    }
    # Expose the value as received for legacy/read-only diagnostics.  Only the
    # typed ConversationItem validates it as an authoritative writable token.
    if revision is not None:
        common["revision"] = revision

    if envelope.event_type == "interaction.requested":
        if interaction_kind == "approval":
            return "approval.requested", {
                **common,
                "approvalId": interaction_id,
                "callId": "",
                "kind": str(presentation.get("title") or interaction_kind),
                "detail": presentation.get("description"),
            }
        return "a2ui.interaction", {
            **common,
            "kind": interaction_kind or "form",
            "inputSchema": (
                dict(request.get("request_schema"))
                if isinstance(request.get("request_schema"), Mapping)
                else {}
            ),
        }
    if envelope.event_type in {
        "interaction.resolved",
        "interaction.cancelled",
        "interaction.expired",
    }:
        outcome = str(
            payload.get("outcome")
            or (
                "cancelled"
                if envelope.event_type == "interaction.cancelled"
                else "expired"
                if envelope.event_type == "interaction.expired"
                else ""
            )
        )
        if interaction_kind == "approval":
            return "approval.resolved", {
                **common,
                "approvalId": interaction_id,
                "callId": "",
                "decision": outcome,
            }
        return "a2ui.action", {**common, "name": outcome}
    return None
