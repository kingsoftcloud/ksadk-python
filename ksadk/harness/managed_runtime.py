"""Canonical RuntimeAdapter for the managed KsADK Harness engine.

Studio and the platform persist canonical RuntimeEvent v2 values, while the
managed engine intentionally owns a richer, engine-internal event vocabulary.
This adapter is the boundary between the two contracts: it keeps the managed
Agent Loop behind the stable ``runtime_type=harness`` product name and projects
the execution tree into canonical status items without teaching Studio about
engine-private Python objects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from ksadk.events.canonical import (
    ApprovalRequest,
    ErrorInfo,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import ContentSnapshot, TextContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine, memory_checkpointer
from ksadk.harness.events import EventType
from ksadk.harness.events import RuntimeEvent as HarnessEvent
from ksadk.harness.execution_policy import ExecutionPolicy, ExecutionPolicyResolver
from ksadk.harness.public_activity import tool_public_action
from ksadk.harness.reasoner import HarnessReasoner
from ksadk.harness.skill_composition import compose_engine
from ksadk.harness.spec import HarnessSpec
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix
from ksadk.kernel.errors import UnsupportedControlError
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


class ManagedHarnessRuntime(BaseRuntime):
    runtime_type = "harness"

    def __init__(self, *, durable: bool = False) -> None:
        self._durable = durable

    def native_capabilities(self) -> dict[str, Any]:
        return {
            "cancel": {"supported": True},
            "resume": {"supported": True},
            "checkpoint": {"supported": True, "granularity": "snapshot"},
            "session_continuity": {
                "durable": self._durable,
                "scope": "workspace" if self._durable else "process",
            },
            "progressive_disclosure": {"skill": True, "mcp": True},
        }


class ManagedHarnessRuntimeAdapter(RuntimeAdapter):
    """Expose :class:`ManagedLangGraphEngine` through the platform adapter API."""

    def __init__(
        self,
        spec: HarnessSpec,
        *,
        reasoner: HarnessReasoner | None = None,
        workspace_root: str | Path = ".",
        engine: ManagedLangGraphEngine | None = None,
        durable: bool = False,
        shared_across_pods: bool = False,
        execution_policy_resolver: ExecutionPolicyResolver | None = None,
    ) -> None:
        super().__init__(ManagedHarnessRuntime(durable=durable))
        self._spec = spec
        self._workspace_root = Path(workspace_root)
        self._durable = durable
        self._shared_across_pods = shared_across_pods
        self._execution_policy_resolver = execution_policy_resolver
        self._engine = engine or compose_engine(
            spec,
            reasoner=reasoner,
            checkpointer=memory_checkpointer(),
            context_engine=HarnessContextEngine(),
        )
        self._compiled: Any | None = None
        self._external_handles: dict[str, RunHandle] = {}
        #: 由装配层注入（持久 Checkpoint 分档时非空）；为空则不持久化 Handle。
        self._run_store: Any | None = None

    def capabilities(self) -> RuntimeCapabilityMatrix:
        return managed_harness_capabilities(
            durable=self._durable,
            execution_policy=self._execution_policy_resolver is not None,
        )

    async def _policy_options(self, request: StartRequest) -> dict[str, Any]:
        ref = request.metadata.get("execution_policy_ref")
        if ref is None:
            return {}
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("execution_policy_ref must be a non-empty host reference")
        if self._execution_policy_resolver is None:
            raise UnsupportedControlError("execution policy requires a trusted host resolver")
        policy = await self._execution_policy_resolver.resolve(ref, request=request)
        if not isinstance(policy, ExecutionPolicy):
            raise TypeError("host resolver must return ExecutionPolicy")
        return {
            "execution_policy": policy,
            "execution_policy_resolver": self._execution_policy_resolver,
            "execution_policy_request": request,
        }

    def _persist_durable_handle(
        self,
        handle: RunHandle,
        status: str = "running",
        *,
        terminal: bool = False,
    ) -> None:
        if self._run_store is None:
            return
        runs, handles = self._run_store.load()
        runs[handle.run_id] = {"runId": handle.run_id, "status": status}
        if terminal:
            handles.pop(handle.run_id, None)
        else:
            handles[handle.run_id] = handle
        self._run_store.save(runs, handles)

    async def attach(self, handle: RunHandle) -> RunHandle:
        """Attach a persisted platform handle to the durable LangGraph thread."""

        if not self._durable:
            raise UnsupportedControlError(
                f"managed Harness run {handle.run_id!r} has no durable checkpoint backend"
            )
        if handle.runtime_type != "harness":
            raise ValueError(f"managed Harness cannot attach {handle.runtime_type!r} handle")
        request = StartRequest(
            input="", user_id=str(handle.native_ref.get("user_id") or "unknown"),
            session_id=handle.session_id, agent_id=handle.native_ref.get("agent_id"),
            metadata={
                "invocation_id": handle.run_id,
                **({"execution_policy_ref": handle.native_ref["execution_policy_ref"]}
                   if "execution_policy_ref" in handle.native_ref else {}),
            },
        )
        options = await self._policy_options(request)
        if handle.run_id in self._external_handles:
            return handle
        compiled = await self._ensure_compiled()
        internal_handle = handle.model_copy(update={"runtime_type": "managed-langgraph"})
        internal = await self._engine.attach(internal_handle, compiled, **options)
        self._external_handles[handle.run_id] = internal
        return handle

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        """Restore a persisted handle and continue non-interactive checkpoints.

        Approval checkpoints deliberately remain ``awaiting_approval`` so the
        Kernel can route the later interaction to the newly attached adapter.
        A process crash during ordinary graph execution is reconstructed as a
        paused checkpoint and must resume automatically; otherwise attach
        succeeds but the durable Run remains open forever.
        """

        restored = await self.attach(handle)
        internal = self._internal_handle(restored)
        state = await self._engine.snapshot_state(internal)
        status = str(getattr(getattr(state, "status", None), "value", ""))
        if status == "paused":
            await self._engine.resume(
                internal,
                ResumeTarget(
                    kind="checkpoint_id",
                    id=str(internal.native_ref.get("thread_id") or internal.run_id),
                ),
                None,
            )
        return restored

    @property
    def harness_spec(self) -> HarnessSpec:
        return self._spec

    async def preflight(self) -> None:
        await self._ensure_compiled()

    async def start(self, request: StartRequest) -> RunHandle:
        compiled = await self._ensure_compiled()
        conversation = request.conversation_preprocessing()
        metadata = dict(request.metadata)
        # 回合级审批档位（composer 完全访问/严格/询问）放在 config 里，
        # 挪进 metadata 让 policy_runtime.configure_run 能按它调整审批面。
        approval_mode = str((request.config or {}).get("tool_approval_mode") or "")
        if approval_mode:
            metadata["tool_approval_mode"] = approval_mode
        if conversation is not None and conversation.messages:
            metadata["conversation_history"] = [dict(item) for item in conversation.messages]
        # Studio 的 Agent 合同给出 max_input_tokens + reserve_output_tokens，
        # ContextEngine 需要完整窗口才能避免退回 32K 安全默认值。该值只作为
        # 本次运行的模型窗口来源，不改变不可变 HarnessSpec。
        if not metadata.get("context_window_tokens"):
            max_input = _positive_int(request.config.get("max_input_tokens"))
            reserve_output = _positive_int(request.config.get("reserve_output_tokens")) or 0
            if max_input is not None:
                metadata["context_window_tokens"] = max_input + reserve_output
        internal_request = request.model_copy(update={"metadata": metadata})
        options = await self._policy_options(internal_request)
        # 始终把请求传给 configure_run：回合级 tool_approval_mode 在那里
        # 覆盖静态审批面（execution_policy_request 仅在有 policy 时传）。
        options.setdefault("execution_policy_request", internal_request)
        internal = await self._engine.start(internal_request, compiled, **options)
        external = internal.model_copy(update={"runtime_type": "harness"})
        self._external_handles[external.run_id] = internal
        self._persist_durable_handle(external)
        return external

    def stream(self, handle: RunHandle) -> AsyncIterator[Any]:
        return self._stream(handle)

    async def _stream(self, handle: RunHandle) -> AsyncIterator[Any]:
        internal = self._internal_handle(handle)
        async for event in self._engine.stream(internal):
            rich = event.to_v2()
            if not rich.parent_run_id:
                terminal_status = {
                    EventType.RUN_COMPLETED: "completed",
                    EventType.RUN_FAILED: "failed",
                    EventType.RUN_CANCELED: "canceled",
                }.get(rich.event_type)
                if terminal_status is not None:
                    # A terminal checkpoint must not remain eligible for cold
                    # attach.  Persist before yielding so a consumer that stops
                    # at the terminal event cannot leave a stale live handle.
                    self._persist_durable_handle(
                        handle,
                        terminal_status,
                        terminal=True,
                    )
            for projected in _project_event(event):
                yield projected

    async def cancel(self, handle: RunHandle) -> CancelResult:
        result = await self._engine.cancel(self._internal_handle(handle))
        if result is CancelResult.INTERRUPTED_ACTIVE_TURN:
            self._persist_durable_handle(handle, "canceled", terminal=True)
        elif result is CancelResult.PENDING_CANCEL_RECORDED:
            self._persist_durable_handle(handle, "cancel_requested")
        return result

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        if payload is not None and payload.kind == "approval_decision":
            decision = payload.data
            if isinstance(decision, dict):
                decision = decision.get("decision")
            # Studio submits action-shaped data; the loop consumes a decision
            # string. Unknown/missing decisions never become an approval.
            normalized = {
                "approve": "approved",
                "approved": "approved",
                "reject": "denied",
                "rejected": "denied",
                "deny": "denied",
                "denied": "denied",
                "cancel": "denied",
                "canceled": "denied",
            }.get(decision if isinstance(decision, str) else "", "denied")
            payload = payload.model_copy(update={"data": normalized})
        internal = await self._engine.resume(self._internal_handle(handle), target, payload)
        external = internal.model_copy(update={"runtime_type": "harness"})
        self._external_handles[external.run_id] = internal
        return external

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        internal = self._internal_handle(handle)
        state = await self._engine.snapshot_state(internal)
        return CheckpointDescriptor(
            checkpoint_id=str(internal.native_ref.get("thread_id") or internal.run_id),
            invocation_id=internal.run_id,
            capability=CheckpointCapability(
                supported=True,
                granularity="snapshot",
                rollback_scope="invocation",
                fork_supported=False,
                durable=self._durable,
                shared_across_pods=self._shared_across_pods,
                reason=(
                    "Managed Harness uses a durable LangGraph checkpointer"
                    if self._durable
                    else "Managed Harness uses an in-process LangGraph checkpointer"
                ),
            ),
            ref={"status": state.status.value if state is not None else "unknown"},
        )

    async def close(self, handle: RunHandle) -> None:
        internal = self._external_handles.pop(handle.run_id, None)
        if internal is not None:
            await self._engine.close(internal)

    def is_handle_attached(self, handle: RunHandle) -> bool:
        internal = self._external_handles.get(handle.run_id)
        return internal is not None and self._engine.is_handle_attached(internal)

    async def _ensure_compiled(self) -> Any:
        if self._compiled is None:
            self._compiled = await self._engine.compile(self._spec)
        return self._compiled

    def _internal_handle(self, handle: RunHandle) -> RunHandle:
        if handle.runtime_type != "harness":
            raise ValueError(f"managed Harness cannot use {handle.runtime_type!r} handle")
        try:
            return self._external_handles[handle.run_id]
        except KeyError:
            raise KeyError(f"unknown managed Harness run: {handle.run_id}") from None


def _project_event(event: HarnessEvent) -> list[Any]:
    """Project one rich Harness event to one or more canonical v2 events."""

    rich = event.to_v2()
    native_run_id = str(rich.run_id or rich.invocation_id)
    run_id = str(rich.invocation_id if rich.parent_run_id else native_run_id)
    scope_id = stable_scope_id("ksadk", native_run_id, str(rich.scope_id or rich.agent_id))
    source = SourceRef(
        framework="ksadk",
        native_event_id=rich.event_id,
        native_run_id=native_run_id,
        metadata={
            "native_event_type": rich.event_type,
            "agent_id": rich.agent_id,
            "session_id": rich.session_id,
            **({"phase": rich.phase} if rich.phase else {}),
            **({"parent_run_id": rich.parent_run_id} if rich.parent_run_id else {}),
        },
    )

    def envelope(event_type: str, item_id: str, part_id: str, ordinal: int = 0) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "event_id": stable_event_id(
                "ksadk", scope_id, item_id, event_type, part_id, rich.event_id, ordinal
            ),
            "seq": rich.seq_id * 10 + ordinal,
            "timestamp": rich.timestamp,
            "run_id": run_id,
            "scope_id": scope_id,
            "parent_scope_id": (
                stable_scope_id("ksadk", run_id, rich.parent_scope_id)
                if rich.parent_scope_id
                else None
            ),
            "source": source,
        }

    def status_projection(details: dict[str, Any] | None = None) -> list[Any]:
        """Keep internal evidence in the canonical trace, not the chat timeline."""
        item_id = stable_item_id("ksadk", run_id, "status", rich.event_id)
        part_id = "status-0"
        text = json.dumps(
            {"event": rich.event_type, "details": rich.payload if details is None else details},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        snapshot = ContentSnapshot(parts=(TextContent(part_id=part_id, text=text),))
        return [
            ItemCompleted(
                **envelope("item.completed", item_id, part_id),
                item_id=item_id,
                item_kind="status",
                snapshot=snapshot,
            )
        ]

    run_item = stable_item_id("ksadk", run_id, "$run")
    payload = rich.payload
    if rich.parent_run_id and rich.event_type in {
        EventType.RUN_STARTED, EventType.RUN_COMPLETED, EventType.RUN_FAILED,
        EventType.RUN_CANCELED, EventType.RUN_INTERRUPTED, EventType.APPROVAL_REQUESTED,
        EventType.TEXT_DELTA, EventType.TEXT_COMPLETED, EventType.REASONING_DELTA,
        EventType.MODEL_CALL_FAILED,
    }:
        return status_projection()
    if rich.event_type == EventType.RUN_STARTED:
        return [RunStarted(**envelope("run.started", run_item, "run"), status="running")]
    if rich.event_type == EventType.RUN_COMPLETED:
        message_id = stable_item_id("ksadk", run_id, "message", "final")
        return [
            RunCompleted(
                **envelope("run.completed", run_item, "run"),
                status="completed",
                output_refs=(OutputRef(scope_id=scope_id, item_id=message_id),),
            )
        ]
    if rich.event_type == EventType.RUN_FAILED:
        return [
            RunFailed(
                **envelope("run.failed", run_item, "run"),
                status="failed",
                error=ErrorInfo(
                    code="HARNESS_EXECUTION_FAILED",
                    message=str(payload.get("error") or "Managed Harness run failed"),
                    source="ksadk.harness.managed",
                    scope_id=scope_id,
                ),
            )
        ]
    if rich.event_type == EventType.RUN_CANCELED:
        return [
            RunCanceled(
                **envelope("run.canceled", run_item, "run"),
                status="canceled",
                reason=str(payload.get("reason") or payload.get("status") or "canceled"),
            )
        ]
    if rich.event_type == EventType.RUN_INTERRUPTED:
        return [
            RunInterrupted(
                **envelope("run.interrupted", run_item, "run"),
                status="interrupted",
                reason=str(payload.get("reason") or "harness_interrupted"),
                interaction_id=(
                    str(payload.get("approval_id") or f"ap-{run_id}")
                    if payload.get("reason") == "tool_approval"
                    else None
                ),
            )
        ]
    # Provider reasoning is retained as trace evidence but is not a stable or
    # user-oriented progress contract. Exposing every model turn creates noisy
    # "thought" cards and may reveal private reasoning. Chat progress comes
    # exclusively from the compact RUN_PROGRESS events below.
    if rich.event_type in {EventType.REASONING_DELTA, EventType.REASONING_COMPLETED}:
        return status_projection()
    # Public commentary is rendered by Studio's chronological activity
    # projection.  Keeping it as a canonical status fact prevents the shared
    # chat reducer from concatenating every checkpoint into the final answer.
    # The final-answer text remains a normal message item below.
    if rich.event_type == EventType.TEXT_COMPLETED and rich.phase == "commentary":
        return status_projection({"text": str(payload.get("text") or "")})
    if rich.event_type == EventType.MODEL_CALL_FAILED:
        if payload.get("action") != "retry_same_model":
            return status_projection()
        return _public_activity_items(
            rich=rich,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            envelope=envelope,
            key=f"provider-retry-{payload.get('attempt') or rich.event_id}",
            summary=_provider_retry_text(payload),
            completed=True,
        )
    # A child Run's text is an input to the parent, not a second assistant
    # answer. Keep it inspectable without rendering it in the parent chat.
    if rich.parent_scope_id and rich.event_type in {
        EventType.TEXT_DELTA,
        EventType.TEXT_COMPLETED,
    }:
        return status_projection()
    if rich.event_type == EventType.RUN_PROGRESS:
        # Child lifecycle transitions remain available in Trace, but showing
        # accepted/running/completed as separate chat rows duplicates the
        # aggregate delegation card and makes parallel work unreadable.
        if payload.get("kind") in {"delegation.route", "subagent.event"}:
            return status_projection()
        kind = str(payload.get("kind") or "")
        if kind == "provider.retry":
            return _public_activity_items(
                rich=rich,
                run_id=run_id,
                scope_id=scope_id,
                source=source,
                envelope=envelope,
                key=f"provider-retry-{rich.event_id}",
                summary=_provider_retry_text(payload),
                completed=True,
            )
        if kind == "delegation.batch":
            status = str(payload.get("status") or "running")
            # One quiet line stays visible; responsibility details live in an
            # expandable reasoning-style card.  The content is a public
            # activity summary, never provider chain-of-thought.
            summary = _delegation_summary(payload)
            detail = _delegation_detail(payload)
            return [
                *_public_activity_items(
                    rich=rich,
                    run_id=run_id,
                    scope_id=scope_id,
                    source=source,
                    envelope=envelope,
                    key="delegation-summary",
                    summary=summary,
                    completed=status != "running",
                    existing=status != "running",
                ),
                *_public_activity_items(
                    rich=rich,
                    run_id=run_id,
                    scope_id=scope_id,
                    source=source,
                    envelope=envelope,
                    key="delegation-detail",
                    summary=detail,
                    completed=status != "running",
                    item_kind="reasoning",
                    existing=status != "running",
                ),
            ]
        progress_completed = str(payload.get("status") or "running") != "running"
        progress_key = (
            f"tool-activity-{payload.get('batch_id') or rich.event_id}"
            if kind == "tool.batch"
            else rich.event_id
        )
        return _public_activity_items(
            rich=rich,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            envelope=envelope,
            key=progress_key,
            summary=_progress_text(payload),
            completed=progress_completed,
            existing=kind == "tool.batch" and progress_completed,
        )
    if rich.event_type == EventType.USAGE_REPORTED:
        input_tokens = max(0, int(payload.get("input_tokens") or 0))
        output_tokens = max(0, int(payload.get("output_tokens") or 0))
        return [
            UsageReported(
                **envelope("usage.reported", run_item, "usage"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cached_tokens=max(0, int(payload.get("cached_tokens") or 0)),
                reasoning_tokens=max(0, int(payload.get("reasoning_tokens") or 0)),
            )
        ]
    if rich.event_type == EventType.APPROVAL_REQUESTED:
        interaction_id = str(payload.get("approval_id") or f"ap-{run_id}")
        return [
            InteractionRequested(
                **envelope("interaction.requested", interaction_id, "approval"),
                interaction_id=interaction_id,
                interaction_kind="approval",
                request=ApprovalRequest(
                    call_id=str(payload.get("call_id") or "") or None,
                    kind=str(payload.get("kind") or "tool"),
                    detail=_public_approval_detail(payload.get("detail")),
                ),
            )
        ]
    if rich.event_type in {EventType.TEXT_COMPLETED, EventType.REASONING_COMPLETED}:
        kind = "message" if rich.event_type == EventType.TEXT_COMPLETED else "reasoning"
        phase = str(rich.phase or ("final_answer" if kind == "message" else "commentary"))
        item_id = (
            stable_item_id("ksadk", run_id, "message", "final")
            if kind == "message" and phase == "final_answer"
            else stable_item_id("ksadk", run_id, kind, rich.event_id)
        )
        part_id = "text-0"
        text = str(payload.get("text") or payload.get("summary") or "")
        content = ContentSnapshot(parts=(TextContent(part_id=part_id, text=text),))
        if payload.get("streamed"):
            return [
                ItemCompleted(
                    **envelope("item.completed", item_id, part_id, 1),
                    item_id=item_id,
                    item_kind=kind,
                    snapshot=content,
                )
            ]
        return [
            ItemStarted(
                **envelope("item.started", item_id, part_id, 0),
                item_id=item_id,
                item_kind=kind,
                phase=phase,
                initial=None,
            ),
            ItemCompleted(
                **envelope("item.completed", item_id, part_id, 1),
                item_id=item_id,
                item_kind=kind,
                snapshot=content,
            ),
        ]
    if rich.event_type in {EventType.TEXT_DELTA, EventType.REASONING_DELTA}:
        kind = "message" if rich.event_type == EventType.TEXT_DELTA else "reasoning"
        phase = str(rich.phase or ("final_answer" if kind == "message" else "commentary"))
        item_id = (
            stable_item_id("ksadk", native_run_id, "message", "final")
            if kind == "message" and phase == "final_answer"
            else stable_item_id("ksadk", run_id, kind, "stream")
        )
        part_id = "text-0"
        text = str(payload.get("text") or "")
        update = TextContent(part_id=part_id, text=text)
        if int(payload.get("delta_index") or 0) == 0:
            return [
                ItemStarted(
                    **envelope("item.started", item_id, part_id, 0),
                    item_id=item_id,
                    item_kind=kind,
                    phase=phase,
                    initial=ContentSnapshot(parts=(update,)),
                )
            ]
        return [
            ItemUpdated(
                **envelope("item.updated", item_id, part_id, int(payload.get("delta_index") or 0)),
                item_id=item_id,
                item_kind=kind,
                op="append",
                update=update,
            )
        ]
    if rich.event_type in {EventType.TOOL_CALL_BEGIN, EventType.TOOL_CALL_END}:
        # Arguments, results and receipts remain in the canonical Trace. The
        # conversation receives the corresponding human-readable tool.batch
        # progress item, avoiding duplicate technical cards and configuration-
        # shaped payloads in the user-facing activity stream.
        action = tool_public_action(payload)
        return status_projection(
            {
                "name": str(payload.get("name") or "tool"),
                "call_id": str(payload.get("call_id") or rich.event_id),
                **({"public_action": action} if action else {}),
                "status": (
                    "started"
                    if rich.event_type == EventType.TOOL_CALL_BEGIN
                    else "failed" if payload.get("error") else "completed"
                ),
            }
        )

    # Preserve the rich execution tree as canonical status items. Studio can
    # render these in the existing Trace inspector without a Harness-only page.
    return status_projection()


def _public_activity_items(
    *,
    rich: Any,
    run_id: str,
    scope_id: str,
    source: Any,
    envelope: Any,
    key: str,
    summary: str,
    completed: bool,
    item_kind: str = "message",
    existing: bool = False,
) -> list[Any]:
    """Build one identity-stable, replaceable public activity item."""
    del rich, scope_id, source
    item_id = stable_item_id("ksadk", run_id, item_kind, f"activity-{key}")
    part_id = "text-0"
    snapshot = ContentSnapshot(parts=(TextContent(part_id=part_id, text=summary),))
    events: list[Any] = []
    if not existing:
        events.append(ItemStarted(
            **envelope("item.started", item_id, part_id, 0),
            item_id=item_id,
            item_kind=item_kind,
            phase="commentary",
            initial=snapshot,
        ))
    events.append(
        ItemUpdated(
            **envelope("item.updated", item_id, part_id, 1),
            item_id=item_id,
            item_kind=item_kind,
            op="replace",
            update=TextContent(part_id=part_id, text=summary),
        )
    )
    if completed:
        events.append(
            ItemCompleted(
                **envelope("item.completed", item_id, part_id, 2),
                item_id=item_id,
                item_kind=item_kind,
                snapshot=snapshot,
            )
        )
    return events


def _progress_text(payload: dict[str, Any]) -> str:
    """Render public execution progress without exposing private chain-of-thought."""
    kind = str(payload.get("kind") or "progress")
    if kind == "plan":
        return f"计划：{_compact_progress_message(payload.get('message'))}"
    if kind == "delegation.batch":
        return _delegation_summary(payload)
    if kind == "tool.batch":
        status = str(payload.get("status") or "running")
        tools = [str(name) for name in payload.get("tools") or []]
        action = _tool_activity_label(tools)
        if status == "failed":
            return f"{action}时遇到问题，正在调整"
        if status == "completed":
            return f"已{action}"
        return f"正在{action}"
    if kind == "provider.retry":
        return _provider_retry_text(payload)
    if kind == "delegation.route":
        provider = "Codex" if "codex" in str(payload.get("provider_ref") or "") else "Harness"
        return f"子任务「{payload.get('label') or '未命名'}」已分派给 {provider}。"
    if kind == "subagent.event":
        status = str(payload.get("status") or "in_progress")
        status_text = {
            "accepted": "等待执行",
            "running": "正在执行",
            "succeeded": "已完成",
            "failed": "执行失败",
            "cancelled": "已取消",
            "interrupted": "已中断",
        }.get(status, status)
        return f"子任务「{payload.get('label') or '未命名'}」：{status_text}。"
    action = str(payload.get("action") or payload.get("status") or "in_progress")
    return f"执行进度：{action}"


def _delegation_summary(payload: dict[str, Any]) -> str:
    count = max(1, int(payload.get("count") or 1))
    status = str(payload.get("status") or "running")
    if status == "failed":
        return f"{count} 个子智能体已结束，部分任务需要调整"
    if status == "completed":
        return f"{count} 个子智能体已完成，正在整理结果"
    return f"{count} 个子智能体正在运行"


def _delegation_detail(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "running")
    labels = [
        str(label).strip() for label in (payload.get("labels") or []) if str(label).strip()
    ]
    marker = "已完成" if status == "completed" else "需要调整" if status == "failed" else "正在运行"
    if not labels:
        return marker
    return "\n".join(f"• {marker}：{label}" for label in labels[:6])


def _provider_retry_text(payload: dict[str, Any]) -> str:
    if payload.get("next_attempt") is not None:
        attempt = max(2, int(payload["next_attempt"]))
    else:
        attempt = max(2, int(payload.get("model_attempt") or 1) + 1)
    maximum = max(attempt, int(payload.get("max_attempts") or 10))
    delay_ms = max(0, int(payload.get("delay_ms") or payload.get("retry_delay_ms") or 0))
    wait = f"，{delay_ms / 1000:g} 秒后" if delay_ms else ""
    label = str(payload.get("label") or "").strip()
    scope = f"（{label}）" if label else ""
    return f"模型服务繁忙{scope}{wait}自动重试（第 {attempt}/{maximum} 次）"


def _public_approval_detail(value: Any) -> dict[str, Any]:
    """Keep approvals actionable without copying document bodies or secrets into chat."""
    detail = dict(value) if isinstance(value, dict) else {}
    arguments = detail.get("arguments") or detail.get("args")
    safe_arguments: dict[str, str] = {}
    if isinstance(arguments, dict):
        for key in ("path", "file_path", "url", "command"):
            raw = arguments.get(key)
            if raw is not None:
                safe_arguments[key] = _compact_progress_message(raw, max_chars=160)
    public: dict[str, Any] = {
        "name": str(detail.get("name") or detail.get("tool_name") or "tool"),
    }
    if safe_arguments:
        public["args"] = safe_arguments
    return public


def _tool_activity_label(tools: list[str]) -> str:
    lowered = " ".join(tools).lower()
    if "write" in lowered or "edit" in lowered or "save" in lowered:
        return "编辑并保存结果"
    if "web_search" in lowered or "search" in lowered:
        return "搜索资料"
    if "web_fetch" in lowered or "fetch" in lowered:
        return "查看资料"
    if "read" in lowered or "list" in lowered:
        return "查看工作区内容"
    if "command" in lowered or "shell" in lowered or "exec" in lowered:
        return "运行任务"
    return "使用工具处理任务"


def _compact_progress_message(value: Any, *, max_chars: int = 120) -> str:
    """Keep only the first useful plan lines for the conversational surface."""
    lines = [line.strip(" -\t") for line in str(value or "计划已生成").splitlines()]
    text = "；".join(line for line in lines if line) or "计划已生成"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip("；，。 ") + "…"


def _compact_responsibilities(labels: list[str], *, max_items: int = 4) -> str:
    if not labels:
        return ""
    visible = labels[:max_items]
    lines = [f"• {label}" for label in visible]
    if len(labels) > max_items:
        lines.append(f"• 另有 {len(labels) - max_items} 项")
    return "\n" + "\n".join(lines)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def managed_harness_capabilities(
    *, durable: bool, execution_policy: bool = False
) -> RuntimeCapabilityMatrix:
    """Return the canonical control matrix for one concrete assembly tier."""

    def available() -> RuntimeCapability:
        return RuntimeCapability(supported=True, mode="native")

    def unavailable(reason: str) -> RuntimeCapability:
        return RuntimeCapability(supported=False, mode="unavailable", reason=reason)

    durable_capability = (
        available() if durable else unavailable("managed_harness_checkpoint_is_process_local")
    )
    return RuntimeCapabilityMatrix(
        cancel=available(),
        pause=unavailable("managed_harness_pause_not_implemented"),
        resume=available(),
        submit_interaction=durable_capability,
        attach=durable_capability,
        steer=unavailable("runtime_no_native_steer"),
        inject=unavailable("runtime_no_native_inject"),
        checkpoint=available(),
        durable_restore=durable_capability,
        interaction_mode="durable_resume" if durable else "unavailable",
        execution_policy=(
            available() if execution_policy else unavailable("managed_harness_no_host_resolver")
        ),
    )


__all__ = [
    "ManagedHarnessRuntime",
    "ManagedHarnessRuntimeAdapter",
    "managed_harness_capabilities",
]
