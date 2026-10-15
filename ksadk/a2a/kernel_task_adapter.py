"""A2A task adapter backed by Server admission and the local Kernel event log."""

from __future__ import annotations

from datetime import datetime
import asyncio
import httpx
from typing import Any

from ksadk.a2a.identity import A2AIngressIdentity
from ksadk.a2a.inbound_kernel import InboundKernelSubmission, ManagedKernelRequestError, submit_verified_inbound, stream_inbound_kernel_facts
from ksadk.a2a.control_plane import A2AControlPlaneError
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.events.canonical import (
    InteractionRequested, RunCanceled, SourceRef,
    StructuredInputRequest, parse_runtime_event,
)
from ksadk.runtime.adapter import RunHandle, CancelResult, ResumeTarget
from ksadk.a2a.resume_store import A2AResumeState


class ManagedKernelA2ATaskAdapter(A2ARuntimeTaskAdapter):
    server_admitted = True
    def __init__(self, runtime_adapter, *, control_plane, session_events, event_dispatcher=None, **kwargs):
        super().__init__(runtime_adapter, **kwargs)
        self._compat_task_adapter = A2ARuntimeTaskAdapter(runtime_adapter, **kwargs)
        self._control_plane = control_plane
        self._session_events = session_events
        self._event_dispatcher = event_dispatcher

    def for_context(self, context):
        identity = self._identity(context)
        identity.validate()
        # Every registry-bound call must use Server admission, including product
        # Key users. Only legacy non-workload calls without a platform Task use
        # the compatibility adapter; runtime callers must never downgrade.
        if identity.caller_principal_type == "runtime" or identity.task_binding is not None:
            return self
        return self._compat_task_adapter

    @staticmethod
    def _identity(context):
        state = getattr(getattr(context, "call_context", None), "state", None)
        identity = state.get("a2a_identity") if isinstance(state, dict) else None
        if not isinstance(identity, A2AIngressIdentity):
            raise PermissionError("managed A2A Kernel requires verified Gateway identity")
        return identity

    async def _submit(self, context, *, continuation=None):
        if self._identity(context).caller_principal_type == "user":
            if self._event_dispatcher is None:
                raise ManagedKernelRequestError("product Kernel fact outbox is unavailable")
            await self._event_dispatcher.ensure_writable()
        current = getattr(context, "current_task", None)
        remote_context_id = str(getattr(current, "context_id", "") or context.context_id or "")
        try:
            result = await submit_verified_inbound(
                self._control_plane, self._identity(context),
                remote_task_id=str(context.task_id or ""), remote_context_id=remote_context_id,
                continuation=continuation,
            )
        except (A2AControlPlaneError, httpx.TransportError, TimeoutError) as exc:
            raise ManagedKernelRequestError("Server A2A admission did not return a confirmed receipt") from exc
        if result.receipt.status not in {"accepted", "duplicate"}:
            raise ManagedKernelRequestError("Server did not accept the A2A command")
        return result

    @staticmethod
    def _remember_submission(handle, result):
        handle.native_ref["kernel_submission"] = {
            "SessionId": result.session_id, "AgentId": result.agent_id,
            "CommandId": str(result.receipt.command_id),
            "Receipt": result.receipt.model_dump(mode="json"),
        }

    async def start_task(self, *, task_id, context, input_data):
        context_session = await self.prepare_context(context)
        result = await self._submit(context)
        stream = stream_inbound_kernel_facts(result, self._session_events)
        try:
            first = await anext(stream)
        finally:
            await stream.aclose()
        handle = RunHandle(run_id=first.run_id, session_id=result.session_id,
                           runtime_type=self._runtime_type,
                           native_ref={"a2a_context_session": context_session, "kernel_stream_after_seq": 0})
        identity = self._identity(context)
        if identity.caller_principal_type == "user":
            handle.native_ref["product_kernel_binding"] = {
                "platform_task_id": identity.task_binding.platform_task_id,
                "target_version_id": identity.task_binding.target_version_id,
                "remote_task_id": str(context.task_id),
                "remote_context_id": str(context.context_id),
            }
        self._remember_submission(handle, result)
        self._handles_by_task_key[self._task_key(task_id, context)] = handle
        await self._resume_state_store.put(self._resume_key(task_id, context), A2AResumeState(
            handle=handle, target=ResumeTarget(kind="invocation_id", id=handle.run_id), payload_kind="free_text",
        ))
        return handle

    def _handle_matches_context(self, handle, context):
        return (handle.runtime_type == self._runtime_type
                and handle.native_ref.get("a2a_context_session") == self._extract_session_id(context)
                and isinstance(handle.native_ref.get("kernel_submission"), dict))

    async def resume_task(self, task_id, context, *, answer):
        await self.prepare_context(context)
        handle, _, _ = await self.validate_resume_task(task_id, context, answer=answer)
        pending = handle.native_ref.get("kernel_pending_interaction")
        if not isinstance(pending, dict) or pending.get("run_id") != handle.run_id:
            raise ValueError("managed A2A task has no durable pending Kernel interaction")
        result = await self._submit(context, continuation=pending)
        if result.session_id != handle.session_id:
            raise PermissionError("Server changed the resumed A2A session")
        if result.receipt.status not in {"accepted", "duplicate"}:
            raise RuntimeError("Server did not accept the A2A continuation")
        handle.native_ref["kernel_stream_after_seq"] = handle.native_ref["kernel_event_cursor"]
        self._remember_submission(handle, result)
        self._handles_by_task_key[self._task_key(task_id, context)] = handle
        return handle

    async def cancel_task(self, task_id, context):
        await self.prepare_context(context)
        identity = self._identity(context)
        identity.validate()
        binding = identity.task_binding
        if binding is None or binding.operation != "cancel_task":
            raise PermissionError("Kernel cancel requires verified Gateway cancel provenance")
        key = self._task_key(task_id, context)
        handle = self._handles_by_task_key.get(key) or await self._restore_handle_from_store(task_id, context)
        if handle is None:
            return CancelResult.NOT_RUNNING
        if not self._handle_matches_context(handle, context):
            raise PermissionError("Kernel cancel handle belongs to another A2A context")
        after_seq = handle.native_ref.get("kernel_event_cursor", 0)
        current = getattr(context, "current_task", None)
        result = await self._control_plane.cancel_inbound_task(
            platform_task_id=binding.platform_task_id, target_version_id=binding.target_version_id,
            remote_task_id=task_id,
            remote_context_id=str(getattr(current, "context_id", "") or context.context_id or ""),
            run_id=handle.run_id,
        )
        if result.session_id != handle.session_id or result.agent_id != identity.target_agent_id:
            raise PermissionError("Server changed the canceled A2A target")
        if result.receipt.status not in {"accepted", "duplicate"}:
            raise RuntimeError("Server did not accept the A2A cancellation")
        async def await_cancel():
            async for fact in stream_inbound_kernel_facts(result, self._session_events,
                    expected_run_id=handle.run_id, after_seq=after_seq, stop_at_waiting=False):
                from ksadk.a2a.product_kernel_facts import enqueue_product_kernel_fact
                await enqueue_product_kernel_fact(self._event_dispatcher, handle, fact)
                if fact.family == "control" and fact.event_type == "control.run_transition" and fact.payload.get("state") == "cancelled":
                    self._handles_by_task_key[key] = handle
                    self._accepted_canceled_tasks.add(key)
                    return CancelResult.INTERRUPTED_ACTIVE_TURN
            return CancelResult.NOT_RUNNING
        return await asyncio.wait_for(await_cancel(), timeout=30)

    async def reconcile_terminal_task(self, task, call_context, task_store):
        """Repair a crash-interrupted protocol task from its owned durable Kernel run."""
        from types import SimpleNamespace

        from a2a.types import TaskState, TaskStatus

        repair = getattr(task_store, "save_confirmed_terminal", None)
        if repair is None:
            return task
        context = SimpleNamespace(current_task=task, task_id=task.id,
                                  context_id=task.context_id, call_context=call_context)
        identity = self._identity(context)
        identity.validate()
        if identity.caller_principal_type != "user" or identity.task_binding is None:
            return task
        await self.prepare_context(context)
        handle = await self._restore_handle_from_store(task.id, context)
        if handle is None or not self._handle_matches_context(handle, context):
            return task
        binding = handle.native_ref.get("product_kernel_binding", {})
        if (binding.get("platform_task_id") != identity.task_binding.platform_task_id
                or binding.get("target_version_id") != identity.task_binding.target_version_id
                or binding.get("remote_task_id") != task.id
                or binding.get("remote_context_id") != task.context_id):
            raise PermissionError("Kernel recovery task binding mismatch")
        submission = InboundKernelSubmission.from_wire(handle.native_ref["kernel_submission"])
        if (submission.session_id != handle.session_id
                or submission.agent_id != identity.target_agent_id):
            raise PermissionError("Kernel recovery session binding mismatch")
        cursor = 0
        while True:
            facts = await self._session_events.read(handle.session_id, cursor, 100)
            if not facts:
                return task
            for fact in facts:
                if (str(fact.run_id) == str(handle.run_id) and fact.family == "control"
                        and fact.event_type == "control.run_transition"
                        and fact.payload.get("state") == "interrupted"):
                    return await repair(task, TaskStatus(state=TaskState.TASK_STATE_FAILED),
                                        call_context)
            next_cursor = facts[-1].seq
            if next_cursor <= cursor:
                raise RuntimeError("Kernel recovery event cursor did not advance")
            cursor = next_cursor

    async def stream_task(self, handle):
        submission = InboundKernelSubmission.from_wire(handle.native_ref["kernel_submission"])
        terminal_seen = False
        pending = None
        async for fact in stream_inbound_kernel_facts(submission, self._session_events,
                expected_run_id=handle.run_id, after_seq=handle.native_ref["kernel_stream_after_seq"]):
            from ksadk.a2a.product_kernel_facts import enqueue_product_kernel_fact
            await enqueue_product_kernel_fact(self._event_dispatcher, handle, fact)
            handle.native_ref["kernel_event_cursor"] = fact.seq
            if fact.family == "runtime":
                event = parse_runtime_event(fact.payload)
                if event.run_id != handle.run_id:
                    raise RuntimeError("Kernel runtime fact contains a conflicting run")
                terminal_seen |= event.event_type in {"run.completed", "run.failed", "run.canceled"}
                yield event
            elif fact.family == "interaction" and fact.event_type == "interaction.requested":
                payload = fact.payload
                kind = payload.get("kind")
                request = payload.get("request", {})
                presentation = request.get("presentation") or {}
                prompt = presentation.get("description") or presentation.get("title") or "Input required"
                if kind == "approval":
                    from ksadk.a2a.interaction_wire import approval_from_presentation

                    projected = approval_from_presentation(
                        call_id=payload["interaction_id"], presentation=presentation,
                    )
                elif kind == "structured_input":
                    projected = StructuredInputRequest(prompt=prompt, schema=request["request_schema"])
                else:
                    raise RuntimeError("A2A cannot project this Kernel interaction kind")
                pending = {"run_id": handle.run_id, "interaction_id": payload["interaction_id"],
                           "expected_revision": payload["revision"], "kind": kind}
                handle.native_ref["kernel_pending_interaction"] = pending
                yield InteractionRequested(
                    schema_version=2, event_id=str(fact.event_id), seq=fact.seq,
                    timestamp=datetime.fromisoformat(fact.timestamp).timestamp(),
                    run_id=handle.run_id, scope_id="run:" + handle.run_id,
                    source=SourceRef(framework="ksadk"), interaction_id=payload["interaction_id"],
                    interaction_kind=kind, request=projected,
                )
            elif fact.event_type == "control.run_transition":
                state = fact.payload.get("state")
                if state == "waiting" and pending is None:
                    raise RuntimeError("Kernel WAITING has no projected pending interaction")
                if state == "cancelled" and not terminal_seen:
                    yield RunCanceled(schema_version=2, event_id=str(fact.event_id), seq=fact.seq,
                        status="canceled",
                        timestamp=datetime.fromisoformat(fact.timestamp).timestamp(),
                        run_id=handle.run_id, scope_id="run:" + handle.run_id,
                        source=SourceRef(framework="ksadk"))
                elif state in {"failed", "interrupted"} and not terminal_seen:
                    raise RuntimeError("Kernel run ended without successful completion")
                elif state == "completed" and not terminal_seen:
                    raise RuntimeError("Kernel completed without a canonical completion fact")
