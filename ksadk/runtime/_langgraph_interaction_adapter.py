"""Private public-wait to native-task mapping and consumption bookkeeping."""

from __future__ import annotations

import copy
import uuid
from ksadk.a2ui.registry import catalog_bound

from ksadk.interaction.native_events import NativeInteractionRequested, NativeSubmissionProgress


class NativeLangGraphInteractionMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._native_started_facts = {}
        self._native_start_requests = {}

    async def start(self, request):
        handle = await super().start(request)
        if self.confirms_native_submissions:
            self._native_start_requests[handle.run_id] = request
        return handle

    async def retire_cancelled_wait(self, handle):
        """Finish a persisted cancellation; never synthesize a resume answer."""
        if not self.confirms_native_submissions or not handle.native_ref.get("native_waits"):
            return
        if not self.is_handle_attached(handle):
            raise ValueError("Native cancellation requires an attached handle")
        from ksadk.runners._langgraph_wait_cancellation import retire_cancelled_wait

        await retire_cancelled_wait(self._runner, handle)

    @catalog_bound
    async def restore_interaction_context(self, handle, requests, events):
        """Recover private native addresses from the verified graph, not public logs.

        AG-UI stores only the opaque checkpoint handle. The task address remains
        in LangGraph's checkpoint; the published occurrence ID proves which
        native wait it belongs to, including repeated waits in the same task.
        """
        if not self.confirms_native_submissions:
            return
        from ksadk.events.canonical import InteractionRequested, ItemCompleted, RunStarted
        from ksadk.events.content import ToolResultContent
        from ksadk.events.identity import stable_item_id
        from ksadk.interaction.langgraph_observer import collect_native_waits

        ref = handle.native_ref
        framework_ref = (ref.get("framework_ref") or {}).get("langgraph") or {}
        expected = {key: str(ref.get(key) or framework_ref.get(key) or "") for key in (
            "thread_id", "checkpoint_ns", "checkpoint_id",
        )}
        address = {key: expected[key] for key in ("thread_id", "checkpoint_ns")}
        snapshot, error = await self._runner._read_graph_state(
            {"configurable": address}, include_subgraphs=True,
        )
        root = (getattr(snapshot, "config", {}) or {}).get("configurable") or {}
        if error or any(str(root.get(key) or "") != expected[key]
                        for key in ("thread_id", "checkpoint_ns", "checkpoint_id")):
            raise ValueError("Native interaction checkpoint changed during restoration")
        for event in events:
            if (isinstance(event, RunStarted) and event.run_id == handle.run_id
                    and event.parent_scope_id is None and event.source.framework == "langgraph"):
                self._native_started_facts[handle.run_id] = event
                break
        waits = collect_native_waits(snapshot)
        def legacy_approval(request):
            if request.interaction_kind != "approval":
                return False
            delivery = request.source.metadata.get("agui_interaction_delivery")
            if delivery is not None:
                return delivery == "legacy_approval"
            # Old Gateway logs can prove their route through the exact
            # approval_required tool-result carrier that produced the request.
            from ksadk.tools.gateway import approval_interrupt_info_from_result

            detail = getattr(request.request, "detail", None)
            if not isinstance(detail, dict):
                return False
            for event in events:
                if (not isinstance(event, ItemCompleted) or event.item_kind != "tool_result"
                        or event.run_id != handle.run_id):
                    continue
                for part in event.snapshot.parts:
                    if not isinstance(part, ToolResultContent) or part.call_id != request.interaction_id:
                        continue
                    evidence = approval_interrupt_info_from_result(part.result)
                    if evidence and evidence["approval_request_id"] == detail.get("approval_request_id"):
                        return True
            return False

        if not waits and all(legacy_approval(request) for request in requests):
            # Gateway/semantic approvals predate native interrupt delivery and
            # intentionally have no task address. Keep their existing resume.
            return
        count = sum(isinstance(event, InteractionRequested) and event.run_id == handle.run_id
                    for event in events)
        unresolved = {request.interaction_id: request for request in requests}
        restored = {}
        for wait in waits:
            native = wait.interrupt
            matches = []
            for generation in range(count + 1):
                public_id = stable_item_id(
                    "langgraph", handle.run_id, "wait", native.native_id,
                    wait.task_id, str(generation),
                )
                if public_id in unresolved:
                    matches.append((public_id, generation))
            # Older approval records kept the native id in their call_id.
            if not matches:
                matches = [(key, 0) for key, request in unresolved.items()
                           if request.interaction_kind == "approval"
                           and getattr(request.request, "call_id", None) == native.native_id]
            if not matches:
                continue
            if len(matches) != 1:
                raise ValueError("Ambiguous native interaction occurrence")
            public_id, generation = matches[0]
            request = unresolved[public_id]
            schema = getattr(request.request, "schema_", None)
            if request.interaction_kind != native.kind or schema != native.schema:
                raise ValueError("Stored input schema differs from its native checkpoint")
            target = {
                **{key: str(root.get(key) or "") for key in (
                    "thread_id", "checkpoint_ns", "checkpoint_id",
                )},
                "call_id": native.native_id, "task_id": wait.task_id,
                "task_namespace": wait.task_namespace, "generation": generation,
                "requested_at": request.timestamp,
            }
            if native.kind == "approval":
                from ksadk.interaction.langgraph_approval import approval_contract

                contract = approval_contract(native.value)
                if contract is not None:
                    target["approval_contract"] = contract
            restored[public_id] = target
        if set(restored) != set(unresolved):
            raise ValueError("Stored interaction does not match a live native task")
        handle.native_ref["native_waits"] = restored

    def restore_native_request(self, handle, record):
        from ksadk.runtime.adapter import StartRequest

        if not self.is_handle_attached(handle):
            raise ValueError("Native request restoration requires an attached checkpoint")
        raw = record.metadata.get("start_request")
        if not isinstance(raw, dict):
            raise ValueError("Native durable run has no original request")
        request = StartRequest.model_validate(raw)
        if (
            request.session_id != record.session_id
            or handle.session_id != record.session_id
            or request.metadata.get("run_id") != record.run_id
            or not record.metadata.get("command_id")
            or request.metadata.get("command_id") != record.metadata["command_id"]
            or handle.run_id != record.metadata.get("runtime_run_id", record.run_id)
            or any(
                handle.native_ref.get(key) != getattr(request, key)
                for key in ("user_id", "agent_id")
                if key in handle.native_ref
            )
        ):
            raise ValueError("Native original request differs from the durable run")
        self._native_start_requests[handle.run_id] = request

    def recovery_hold_reason(self, handle, record):
        if (
            self.confirms_native_submissions
            and handle.native_ref.get("native_waits")
            and handle.native_ref.get("managed_a2a_takeover") is not True
            and record.state.value != "waiting"
        ):
            # A crash can fall after a tool effect and before its consumption
            # fact. Never turn this opaque attachment into stream(input="").
            return "native_submission_requires_reconciliation"
        return None

    async def resume(self, handle, target, payload):
        request = self._native_start_requests.get(handle.run_id)
        if request is not None and handle.native_ref.get("native_waits"):
            from ksadk.runtime.adapter import RESUME_START_REQUEST_NATIVE_KEY

            handle.native_ref[RESUME_START_REQUEST_NATIVE_KEY] = request.model_dump(mode="python")
        return await super().resume(handle, target, payload)

    def _original_start_fact(self, handle):
        if not self.confirms_native_submissions:
            return super()._original_start_fact(handle)
        if handle.run_id not in self._native_started_facts:
            self._native_started_facts[handle.run_id] = self._make_run_started(handle)
        return self._native_started_facts[handle.run_id]

    async def close(self, handle):
        try:
            await super().close(handle)
        finally:
            self._native_start_requests.pop(handle.run_id, None)
            self._native_started_facts.pop(handle.run_id, None)
            root = handle.native_ref.get("_ksadk_public_root", {})
            self._native_started_facts.pop(root.get("run_id"), None)

    async def checkpoint(self, handle):
        if handle.native_ref.get("_native_checkpoint_error"):
            self._require_native_checkpoint_capability()
            raise RuntimeError("Completed LangGraph checkpoint could not be read")
        return await super().checkpoint(handle)

    @property
    def confirms_native_submissions(self):
        return bool(getattr(self._runner, "prefers_canonical_stream", False))

    def remember_native_interactions(self, handle, records):
        pending = handle.native_ref.setdefault("native_waits", {})
        for record in records:
            if record.provider_id == "langgraph" and (record.native_target or {}).get("task_id"):
                pending[record.interaction_id] = copy.deepcopy(record.native_target)

    def _native_wait_resume(self, handle, target, payload):
        pending = handle.native_ref.get("native_waits", {})
        public_id = payload.call_id if payload is not None else None
        native = pending.get(public_id)
        submitted = {public_id: native} if native is not None else {}
        batch = payload is not None and public_id is None and isinstance(payload.data, dict)
        if batch and pending:
            if not payload.data or not set(payload.data).issubset(pending):
                raise ValueError("Batch resume contains unknown native waits")
            submitted = {key: pending[key] for key in payload.data}
            native = next(iter(submitted.values()))
        if not submitted:
            return None
        if target.kind != "checkpoint_id" or any(
            target.id != value["checkpoint_id"] for value in submitted.values()
        ):
            raise ValueError("Native wait does not belong to the requested checkpoint")
        attempt = str(handle.native_ref.pop("native_submission_key", None) or uuid.uuid4())
        return {
            **(
                self._native_start_requests[handle.run_id].config
                if handle.run_id in self._native_start_requests
                else {}
            ),
            **(
                {
                    "metadata": dict(self._native_start_requests[handle.run_id].metadata),
                    "model": self._native_start_requests[handle.run_id].model,
                }
                if handle.run_id in self._native_start_requests
                else {}
            ),
            "checkpoint_resume": True,
            "run_id": handle.run_id,
            "resume_payload_provided": True,
            "resume_interrupt_id": "" if batch else native["call_id"],
            "framework_ref": {
                "langgraph": {
                    key: native[key] for key in ("checkpoint_id", "thread_id", "checkpoint_ns")
                }
            },
            "input": (
                {value["call_id"]: payload.data[key] for key, value in submitted.items()}
                if batch
                else payload.data
            ),
            "_ksadk_native_resume": {
                "attempt_key": attempt,
                "submitted": copy.deepcopy(submitted),
                "pending": copy.deepcopy(pending),
            },
        }

    def _resume_identity(self, handle, target, payload, override):
        native = (override or {}).get("_ksadk_native_resume")
        if native:
            return (
                handle.run_id,
                handle.session_id,
                "native_wait",
                ",".join(sorted(native["submitted"])),
                native["attempt_key"],
            )
        return super()._resume_identity(handle, target, payload, override)

    def _resume_consumed_on_start(self, run):
        return not bool(run.resume_key and run.resume_key[2] == "native_wait")

    def _observe_runner_event(self, handle, run, event):
        from ksadk.events.native_checkpoint import NativeCheckpointCompleted

        if isinstance(event, NativeCheckpointCompleted):
            if event._checkpoint_error:
                handle.native_ref["_native_checkpoint_error"] = True
                handle.native_ref.pop("checkpoint_id", None)
                framework = handle.native_ref.get("framework_ref") or {}
                previous = dict(framework.get("langgraph") or {})
                previous.pop("checkpoint_id", None)
                handle.native_ref["framework_ref"] = {**framework, "langgraph": previous}
            elif event._checkpoint_ref is not None:
                ref = dict(event._checkpoint_ref)
                handle.native_ref.update(ref)
                handle.native_ref["framework_ref"] = {
                    **handle.native_ref.get("framework_ref", {}), "langgraph": ref,
                }
                known = handle.native_ref.setdefault("known_checkpoint_ids", [])
                if ref["checkpoint_id"] not in known:
                    known.append(ref["checkpoint_id"])
                handle.native_ref.pop("_native_checkpoint_error", None)
        if isinstance(event, NativeInteractionRequested):
            handle.native_ref.pop("_native_checkpoint_error", None)
            handle.native_ref.setdefault("native_waits", {})[event.interaction_id] = copy.deepcopy(
                event._native_target
            )
        if isinstance(event, NativeSubmissionProgress):
            for outcome in event._outcomes:
                if outcome.status == "consumed":
                    target = handle.native_ref.get("native_waits", {}).pop(outcome.interaction_id, None)
                    if target and target.get("legacy_a2ui_binding"):
                        bindings = handle.native_ref.get("a2ui_presentation", {}).get("interactions", {})
                        sid = target.get("a2ui_surface_id")
                        if bindings.get(sid) == outcome.interaction_id:
                            bindings.pop(sid)
                    pending = handle.native_ref.get("pending_approval_ids", [])
                    if outcome.interaction_id in pending:
                        pending.remove(outcome.interaction_id)
            if run.resume_key is not None and all(
                outcome.status == "consumed" for outcome in event._outcomes
            ):
                self._consumed_resumes.add(run.resume_key)


__all__ = ["NativeLangGraphInteractionMixin"]
