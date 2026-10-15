"""ADK / LangGraph 两个框架的 RuntimeAdapter (goal-07)。

在通用 :class:`~ksadk.runtime.runner_adapter.RunnerRuntimeAdapter` 之上,只声明
框架差异(resume 目标 + checkpoint 粒度),诚实暴露,不开接口后门:

- ADK:**forward-only**,resume 经 ``invocation_id``;不支持 time-travel/fork。
- LangGraph:**time-travel**,resume 经 ``checkpoint_id``;可按 turn 回滚/fork。

默认注册由 :mod:`ksadk.runtime.factory` 统一负责。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import (
    CheckpointCapability,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeCapability,
    StartRequest,
)
from ksadk.runtime.preprocessing import prepare_runtime_start
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter, _ActiveRun
from ksadk.runtime_context import TRUSTED_IDENTITY_METADATA_KEY, session_invocation_context

if TYPE_CHECKING:
    from ksadk.kernel.store import RunRecord


class ADKRuntimeAdapter(RunnerRuntimeAdapter):
    """ADK 框架 adapter(forward-only resume)。"""

    # Native resume must re-enter the graph to reconstruct live managed A2A
    # waiters. Merely attaching a WAITING handle cannot accept their replies.
    recovery_requires_stream = True

    def __init__(self, runner: BaseRunner) -> None:
        super().__init__(runner, runtime_type="adk")

    async def restore_run_record(self, handle: RunHandle, record: "RunRecord") -> None:
        """Rebuild private request context after native attachment has succeeded.

        A handle alone cannot reconstruct the original identity or distinguish
        native resume from a new user turn. Legacy records without a snapshot
        must fail closed instead of silently starting another invocation.
        """
        if not self.is_handle_attached(handle):
            raise ValueError("ADK request restoration requires native attachment")
        raw = record.metadata.get("start_request")
        if not isinstance(raw, dict):
            raise ValueError("ADK durable run has no original start request")
        request = StartRequest.model_validate(raw)
        if (
            request.session_id != record.session_id
            or handle.session_id != record.session_id
            or request.metadata.get("run_id") != record.run_id
            or not record.metadata.get("command_id")
            or request.metadata.get("command_id") != record.metadata.get("command_id")
            or handle.run_id != record.metadata.get("runtime_run_id", record.run_id)
        ):
            raise ValueError("ADK original start request does not match durable run")
        for key in ("user_id", "agent_id"):
            if key in handle.native_ref and handle.native_ref[key] != getattr(request, key):
                raise ValueError("ADK original identity does not match native handle")
        if handle.run_id in self._active_runs:
            raise ValueError("ADK durable restoration cannot replace a live invocation")
        prepared = await prepare_runtime_start(request, self._runner)
        run = _ActiveRun(invocation_id=handle.run_id, session_id=handle.session_id)
        run.__dict__["_restored_from_record"] = True
        run.__dict__["_start_request"] = request
        if prepared is not None:
            run.__dict__["_prepared_start"] = prepared
        run.__dict__["_resume_input_override"] = {
            **request.config,
            "metadata": dict(request.metadata),
            "checkpoint_resume": True,
            "run_id": handle.run_id,
            "invocation_id": handle.run_id,
            "framework_ref": {},
            "input": None,
        }
        snapshot = request.metadata.get("session_context")
        identity = request.metadata.get(TRUSTED_IDENTITY_METADATA_KEY)
        if snapshot is not None or identity is not None or request.metadata.get("tool_approval_mode"):
            run.__dict__["_tag_context"] = session_invocation_context(
                snapshot,
                agent_id=request.agent_id or "",
                user_id=request.user_id,
                session_id=request.session_id,
                runner_type="adk",
                identity=identity,
                tool_approval_mode=str(request.metadata.get("tool_approval_mode") or ""),
            )
        self._active_runs[handle.run_id] = run

    def capabilities(self):
        matrix = super().capabilities()
        bootstrap = getattr(self._runner, "_managed_a2a_bootstrap", None)
        if bootstrap is None or not bootstrap.outbound_enabled:
            return matrix
        # The Kernel owns this live approval channel. Native ADK interaction
        # delivery remains unavailable; admission is restricted to its ledger
        # records, and Worker requires the original live invocation waiter.
        return matrix.model_copy(
            update={
                "submit_interaction": RuntimeCapability(
                    supported=True, mode="emulated", reason="managed_a2a_live_interaction_only"
                ),
                "interaction_mode": "live_submit",
            }
        )

    async def _resume_native(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> Optional[dict]:
        # ADK forward-only:恢复目标 = invocation_id。注入 adk_runner 真消费的
        # ``checkpoint_resume`` + ``framework_ref.adk.invocation_id`` → run_async(invocation_id)。
        runner_input = {
            "checkpoint_resume": True,
            "run_id": target.id,
            "framework_ref": {"adk": {"invocation_id": target.id}},
            "input": payload.data if payload else None,
        }
        if payload is not None:
            runner_input["resume_payload_kind"] = payload.kind
            runner_input["resume_call_id"] = payload.call_id
        return runner_input

    def _checkpoint_capability(self) -> CheckpointCapability:
        capability = super()._checkpoint_capability()
        if not capability.supported:
            return capability
        return capability.model_copy(
            update={
                "granularity": "delta",
                "rollback_scope": "invocation",
                "fork_supported": False,
            }
        )


class LangGraphRuntimeAdapter(RunnerRuntimeAdapter):
    """LangGraph 框架 adapter(time-travel resume)。"""

    def __init__(self, runner: BaseRunner) -> None:
        super().__init__(runner, runtime_type="langgraph")

    def requires_recovery_stream(self, handle: RunHandle) -> bool:
        """A shared adapter only restarts the restored managed invocation."""
        run = self._active_runs.get(handle.run_id)
        return bool(
            run is not None
            and run.session_id == handle.session_id
            and run.__dict__.get("_restored_from_record")
            and handle.native_ref.get("managed_a2a_takeover") is True
        )

    @property
    def requires_durable_start_request(self):
        bootstrap = getattr(self._runner, "_managed_a2a_bootstrap", None)
        return bootstrap is not None and bootstrap.outbound_enabled

    def remember_managed_checkpoint(self, handle, event):
        from ksadk.events.canonical import ContinuationCreated

        bootstrap = getattr(self._runner, "_managed_a2a_bootstrap", None)
        if (
            bootstrap is None or not bootstrap.outbound_enabled
            or not isinstance(event, ContinuationCreated)
            or event.source.framework != "langgraph"
            or event.source.metadata.get("managed_a2a_takeover") is not True
            or event.continuation_kind != "graph_checkpoint" or not event.resumable
        ):
            return False
        if not event.ref.get("checkpoint_id") or not event.ref.get("thread_id"):
            raise ValueError("A2A_NATIVE_RECOVERY_CHECKPOINT_REQUIRED")
        ref = {key: str(event.ref.get(key) or "")
               for key in ("checkpoint_id", "thread_id", "checkpoint_ns")}
        handle.native_ref.update(ref)
        handle.native_ref["framework_ref"] = {"langgraph": ref}
        handle.native_ref["managed_a2a_takeover"] = True
        return True

    async def restore_run_record(self, handle: RunHandle, record: "RunRecord") -> None:
        if handle.native_ref.get("managed_a2a_takeover") is not True:
            return
        bootstrap = getattr(self._runner, "_managed_a2a_bootstrap", None)
        if bootstrap is None or not bootstrap.outbound_enabled or not self.is_handle_attached(handle):
            raise ValueError("LangGraph managed restoration requires native attachment")
        raw = record.metadata.get("start_request")
        if not isinstance(raw, dict):
            raise ValueError("LangGraph durable run has no original start request")
        request = StartRequest.model_validate(raw)
        if (
            request.session_id != record.session_id or handle.session_id != record.session_id
            or request.metadata.get("run_id") != record.run_id
            or not record.metadata.get("command_id")
            or request.metadata.get("command_id") != record.metadata.get("command_id")
            or handle.run_id != record.metadata.get("runtime_run_id", record.run_id)
        ):
            raise ValueError("LangGraph original start request does not match durable run")
        for key in ("user_id", "agent_id"):
            if key in handle.native_ref and handle.native_ref[key] != getattr(request, key):
                raise ValueError("LangGraph original identity does not match native handle")
        if handle.run_id in self._active_runs:
            raise ValueError("LangGraph restoration cannot replace a live invocation")
        prepared = await prepare_runtime_start(request, self._runner)
        run = _ActiveRun(invocation_id=handle.run_id, session_id=handle.session_id)
        run.__dict__.update(_restored_from_record=True, _start_request=request)
        if prepared is not None:
            run.__dict__["_prepared_start"] = prepared
        run.__dict__["_resume_input_override"] = {
            **request.config, "metadata": dict(request.metadata),
            "checkpoint_resume": True, "_ksadk_managed_takeover": True,
            "run_id": handle.run_id, "invocation_id": handle.run_id,
            "framework_ref": handle.native_ref["framework_ref"], "input": None,
        }
        snapshot = request.metadata.get("session_context")
        identity = request.metadata.get(TRUSTED_IDENTITY_METADATA_KEY)
        if snapshot is not None or identity is not None or request.metadata.get("tool_approval_mode"):
            run.__dict__["_tag_context"] = session_invocation_context(
                snapshot, agent_id=request.agent_id or "", user_id=request.user_id,
                session_id=request.session_id, runner_type="langgraph", identity=identity,
                tool_approval_mode=str(request.metadata.get("tool_approval_mode") or ""),
            )
        self._active_runs[handle.run_id] = run

    def capabilities(self):  # noqa: ANN201
        """Advertise checkpoint interaction only when LangGraph can resume it.

        ``RunnerRuntimeAdapter`` is intentionally conservative because a
        generic checkpoint is not proof of original-interrupt delivery.  The
        LangGraph adapter owns that mapping, so it is the only runner-family
        adapter that may publish ``durable_resume``.
        """

        matrix = super().capabilities()
        updates = {
            "interaction_mode": "durable_resume" if matrix.resume.supported else "unavailable"
        }
        bootstrap = getattr(self._runner, "_managed_a2a_bootstrap", None)
        if bootstrap is not None and bootstrap.outbound_enabled:
            # Managed waiters are delivered by Kernel, independently of the
            # native checkpoint provider. Keep native interrupt/resume intact.
            updates["submit_interaction"] = RuntimeCapability(
                supported=True, mode="emulated", reason="managed_a2a_live_interaction_only"
            )
        return matrix.model_copy(update=updates)

    async def _resume_native(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> Optional[dict]:
        if target.kind != "checkpoint_id":
            raise ValueError(f"LangGraph resume requires checkpoint_id target, got {target.kind!r}")
        known_checkpoint_ids = {
            str(checkpoint_id)
            for checkpoint_id in handle.native_ref.get("known_checkpoint_ids", [])
        }
        if target.id not in known_checkpoint_ids:
            raise ValueError(f"checkpoint {target.id!r} does not belong to run {handle.run_id!r}")
        if payload is not None and payload.call_id:
            pending_approval_ids = {
                str(call_id) for call_id in handle.native_ref.get("pending_approval_ids", [])
            }
            if payload.call_id not in pending_approval_ids:
                raise ValueError(f"unknown interrupt {payload.call_id!r}")
        # LangGraph time-travel:恢复目标 = checkpoint_id。注入 langgraph_runner 真消费的
        # ``checkpoint_resume`` + ``framework_ref.langgraph.{checkpoint_id,thread_id}``。
        return {
            "checkpoint_resume": True,
            "run_id": handle.run_id,
            "resume_payload_provided": payload is not None,
            "resume_interrupt_id": payload.call_id if payload else None,
            "framework_ref": {
                "langgraph": {
                    "checkpoint_id": target.id,
                    "thread_id": str(handle.native_ref.get("thread_id") or handle.session_id),
                    **({"checkpoint_ns": str(handle.native_ref["checkpoint_ns"])}
                       if "checkpoint_ns" in handle.native_ref else {}),
                }
            },
            "input": payload.data if payload else None,
        }

    def _checkpoint_capability(self) -> CheckpointCapability:
        capability = super()._checkpoint_capability()
        if not capability.supported:
            describe = getattr(self._runner, "describe_checkpoint_capability", None)
            try:
                raw = dict(describe()) if callable(describe) else {}
            except Exception:  # noqa: BLE001
                raw = {}
            if (
                str(raw.get("Scope") or "").strip().lower() == "process_local"
                and str(raw.get("Backend") or "").strip().lower() in {"memory", "sqlite"}
            ):
                # The runner correctly refuses to advertise this checkpoint to
                # hosted callers because it is not durable across a process
                # restart. This adapter only resumes an attached in-process
                # handle, for which LangGraph's memory saver is sufficient.
                capability = capability.model_copy(
                    update={
                        "supported": True,
                        "reason": (
                            "Checkpoint resume is limited to the current process; "
                            "it is not durable across restarts or pods"
                        ),
                    }
                )
        if not capability.supported:
            return capability
        return capability.model_copy(
            update={
                "granularity": "snapshot",
                "rollback_scope": "turn",
                "fork_supported": True,
            }
        )


__all__ = [
    "ADKRuntimeAdapter",
    "LangGraphRuntimeAdapter",
]
