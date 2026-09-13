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
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import ContentSnapshot, TextContent, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine, memory_checkpointer
from ksadk.harness.events import EventType
from ksadk.harness.events import RuntimeEvent as HarnessEvent
from ksadk.harness.execution_policy import (
    ExecutionPolicy,
    ExecutionPolicyResolver,
    apply_execution_policy,
)
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
        return managed_harness_capabilities(durable=self._durable).model_copy(
            update={
                "execution_policy": RuntimeCapability(
                    supported=self._execution_policy_resolver is not None,
                    mode="native" if self._execution_policy_resolver is not None else "unavailable",
                    reason=(
                        None
                        if self._execution_policy_resolver is not None
                        else "execution_policy_resolver_unavailable"
                    ),
                )
            }
        )

    async def _execution_options(self, request: StartRequest) -> tuple[Any, dict[str, Any]]:
        ref = request.metadata.get("execution_policy_ref")
        if ref is None:
            return await self._ensure_compiled(), {}
        if not isinstance(ref, str) or not ref.strip() or self._execution_policy_resolver is None:
            raise UnsupportedControlError("execution policy reference requires a host resolver")
        policy = await self._execution_policy_resolver.resolve(ref, request=request)
        if not isinstance(policy, ExecutionPolicy):
            raise ValueError("execution policy resolver returned an invalid policy")
        collisions = set(policy.tools) & (
            set(self._engine._tools) | {binding.name for binding in self._spec.sub_agents}
        )
        if collisions:
            raise ValueError(f"execution policy tool collision: {sorted(collisions)}")
        spec = apply_execution_policy(self._spec, policy)
        tools = {**self._engine._tools, **policy.tools}
        return await self._engine.compile(spec, tools=tools), {
            "tools": tools,
            "execution_policy": policy,
            "execution_policy_resolver": self._execution_policy_resolver,
            "execution_policy_request": request,
        }

    def _persist_durable_handle(self, handle: RunHandle, status: str = "running") -> None:
        if self._run_store is None:
            return
        runs, handles = self._run_store.load()
        runs[handle.run_id] = {"runId": handle.run_id, "status": status}
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
        if handle.run_id in self._external_handles:
            return handle
        request = StartRequest(
            input="",
            agent_id=handle.native_ref.get("agent_id"),
            user_id=str(handle.native_ref.get("user_id") or "unknown"),
            session_id=handle.session_id,
            metadata=(
                {
                    "execution_policy_ref": handle.native_ref["execution_policy_ref"],
                    "run_id": handle.native_ref.get("execution_policy_run_id") or handle.run_id,
                }
                if "execution_policy_ref" in handle.native_ref
                else {}
            ),
        )
        compiled, options = await self._execution_options(request)
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
        compiled, options = await self._execution_options(request)
        conversation = request.conversation_preprocessing()
        metadata = dict(request.metadata)
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
        internal = await self._engine.start(internal_request, compiled, **options)
        if "execution_policy_ref" in metadata:
            internal = internal.model_copy(
                update={
                    "native_ref": {
                        **internal.native_ref,
                        "execution_policy_ref": metadata["execution_policy_ref"],
                        "execution_policy_run_id": metadata.get("run_id") or internal.run_id,
                    }
                }
            )
            self._engine._runs[internal.run_id].handle = internal
        external = internal.model_copy(update={"runtime_type": "harness"})
        self._external_handles[external.run_id] = internal
        self._persist_durable_handle(external)
        return external

    def stream(self, handle: RunHandle) -> AsyncIterator[Any]:
        return self._stream(handle)

    async def _stream(self, handle: RunHandle) -> AsyncIterator[Any]:
        internal = self._internal_handle(handle)
        async for event in self._engine.stream(internal):
            for projected in _project_event(event):
                yield projected

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return await self._engine.cancel(self._internal_handle(handle))

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
    run_id = str(rich.invocation_id)
    is_child = bool(rich.parent_run_id and rich.parent_run_id != native_run_id)
    scope_id = stable_scope_id("ksadk", native_run_id, str(rich.scope_id or rich.agent_id))
    source = SourceRef(
        framework="ksadk",
        native_event_id=rich.event_id,
        native_run_id=native_run_id,
        metadata={
            "native_event_type": rich.event_type,
            "agent_id": rich.agent_id,
            "session_id": rich.session_id,
            "parent_run_id": rich.parent_run_id,
            "native_seq": rich.payload.get("source_seq", rich.seq_id),
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
                stable_scope_id("ksadk", rich.parent_run_id or native_run_id, rich.parent_scope_id)
                if rich.parent_scope_id
                else None
            ),
            "source": source,
        }

    run_item = stable_item_id("ksadk", run_id, "$run")
    payload = rich.payload
    if is_child and (
        rich.event_type.startswith("run.") or rich.event_type == EventType.APPROVAL_REQUESTED
    ):
        # Kernel owns the outer Run. Preserve typed native lifecycle facts in a
        # child scope without ever issuing a terminal/interaction for the root.
        item_id = stable_item_id("ksadk", native_run_id, "status", rich.event_id)
        return [
            ItemCompleted(
                **envelope("item.completed", item_id, "status-0"),
                item_id=item_id,
                item_kind="status",
                snapshot=ContentSnapshot(
                    parts=(
                        TextContent(
                            part_id="status-0",
                            text=json.dumps(
                                {
                                    "event": rich.event_type,
                                    "child_run_id": native_run_id,
                                    "parent_run_id": rich.parent_run_id,
                                    "details": payload,
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                        ),
                    )
                ),
            )
        ]
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
                    detail=payload.get("detail"),
                ),
            )
        ]
    if rich.event_type in {EventType.TEXT_COMPLETED, EventType.REASONING_COMPLETED}:
        kind = "message" if rich.event_type == EventType.TEXT_COMPLETED else "reasoning"
        phase = "final_answer" if kind == "message" and not is_child else "commentary"
        item_id = (
            stable_item_id("ksadk", native_run_id, "message", "final")
            if kind == "message"
            else stable_item_id("ksadk", run_id, kind, rich.event_id)
        )
        part_id = "text-0"
        text = str(payload.get("text") or payload.get("summary") or "")
        content = ContentSnapshot(parts=(TextContent(part_id=part_id, text=text),))
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
    if rich.event_type in {EventType.TOOL_CALL_BEGIN, EventType.TOOL_CALL_END}:
        call_id = str(payload.get("call_id") or rich.event_id)
        name = str(payload.get("name") or "tool")
        if rich.event_type == EventType.TOOL_CALL_BEGIN:
            item_id = stable_item_id("ksadk", native_run_id, "tool_call", call_id)
            part_id = "tool-call-0"
            content: Any = ToolCallContent(
                part_id=part_id,
                call_id=call_id,
                name=name,
                arguments=payload.get("args") or {},
            )
            item_kind = "tool_call"
        else:
            item_id = stable_item_id("ksadk", native_run_id, "tool_result", call_id)
            part_id = "tool-result-0"
            content = ToolResultContent(
                part_id=part_id,
                call_id=call_id,
                result=(
                    {"error": payload["error"]}
                    if payload.get("error")
                    else payload.get("result", {})
                ),
                is_error=bool(payload.get("error")),
            )
            item_kind = "tool_result"
        snapshot = ContentSnapshot(parts=(content,))
        return [
            ItemStarted(
                **envelope("item.started", item_id, part_id, 0),
                item_id=item_id,
                item_kind=item_kind,
                phase="commentary",
                initial=snapshot,
            ),
            ItemCompleted(
                **envelope("item.completed", item_id, part_id, 1),
                item_id=item_id,
                item_kind=item_kind,
                snapshot=snapshot,
            ),
        ]

    # Preserve the rich execution tree as canonical status items. Studio can
    # render these in the existing Trace inspector without a Harness-only page.
    item_id = stable_item_id("ksadk", run_id, "status", rich.event_id)
    part_id = "status-0"
    text = json.dumps(
        {"event": rich.event_type, "details": payload},
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


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def managed_harness_capabilities(*, durable: bool) -> RuntimeCapabilityMatrix:
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
        # The Kernel's Interaction command routes through the declared
        # durable-resume provider; it does not call adapter.submit().
        submit_interaction=durable_capability,
        attach=durable_capability,
        steer=unavailable("runtime_no_native_steer"),
        inject=unavailable("runtime_no_native_inject"),
        checkpoint=available(),
        durable_restore=durable_capability,
        interaction_mode="durable_resume" if durable else "unavailable",
    )


__all__ = [
    "ManagedHarnessRuntime",
    "ManagedHarnessRuntimeAdapter",
    "managed_harness_capabilities",
]
