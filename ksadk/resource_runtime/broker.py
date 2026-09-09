"""Resource dispatch admission; transport and subprocess ownership stay separate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from ksadk.resource_runtime.discovery_receipts import (
    DiscoverySelectionReceipts,
    DiscoverySkillReceipt,
)
from ksadk.resource_runtime.ipc import FrameError, ResourceRequest, encode_frame
from ksadk.resource_runtime.knowledge import KnowledgeQuery
from ksadk.resource_runtime.leases import LeaseRejected, ResourceLeaseRegistry, ResourceScope
from ksadk.resource_runtime.memory_provider import (
    MemoryQuery,
    MemoryRecordSelector,
    MemoryStatusQuery,
    MemoryUpdate,
    MemoryWrite,
)
from ksadk.resource_runtime.memory_receipts import MemoryMutationReceipt
from ksadk.resource_runtime.operation_ledger import OperationConflict, OperationLedger
from ksadk.resource_runtime.skill_artifacts import SkillArtifactQuery, SkillArtifactReceipts
from ksadk.resource_runtime.skills import SkillExecutionQuery


class ResourceWorkerPort(Protocol):
    async def invoke(self, request: ResourceRequest, scope: ResourceScope) -> dict: ...


class ResourceWriteAuthorizer(Protocol):
    async def authorize(self, request: ResourceRequest, scope: ResourceScope) -> str | None:
        """Resolve a host approval bound to exact arguments and return its stable event ID.

        The implementation must validate the request schema and host minimum policy,
        and look up approval in trusted state. IPC arguments are never approval.
        Return None to deny. The event ID must survive activation/checkpoint recovery.
        """
        ...


@dataclass
class _Activation:
    worker: ResourceWorkerPort
    scopes: dict[str, ResourceScope]
    tasks: set[asyncio.Task] = field(default_factory=set)
    closed: bool = False
    execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    artifact_roots: dict[str, Path] = field(default_factory=dict)
    discovery_keys: dict[str, str] = field(default_factory=dict)


_WRITES = frozenset({"save_memory", "update_memory", "delete_memory", "execute_skills"})


class ResourceBroker:
    """One event-loop-owned dispatcher for a Core generation.

    Registration is host-only. The worker receives identity resolved by the
    registry; IPC callers cannot register workers, set identity or mint handles.
    Write dispatch remains closed until the Kernel approval/receipt adapter is
    installed; a scope alone is never approval for side effects.
    """

    def __init__(
        self,
        registry: ResourceLeaseRegistry,
        generation_id: str,
        *,
        clock: Callable[[], float] = time.time,
        max_inflight: int = 8,
        write_authorizer: ResourceWriteAuthorizer | None = None,
        operation_ledger: OperationLedger | None = None,
    ):
        if type(max_inflight) is not int or not 1 <= max_inflight <= 8:
            raise ValueError("Resource concurrency must be 1..8")
        self._registry = registry
        self._generation_id = generation_id
        self._clock = clock
        self._max_inflight = max_inflight
        if (write_authorizer is None) != (operation_ledger is None):
            raise ValueError("Write authorization and durable ledger must be configured together")
        self._write_authorizer = write_authorizer
        self._operation_ledger = operation_ledger
        self._discovery_receipts = (
            DiscoverySelectionReceipts(operation_ledger) if operation_ledger is not None else None
        )
        self._skill_artifacts = (
            SkillArtifactReceipts(operation_ledger) if operation_ledger is not None else None
        )
        self._activations: dict[str, _Activation] = {}

    def register(
        self,
        scope: ResourceScope,
        worker: ResourceWorkerPort,
        *,
        artifact_root: Path | None = None,
        discovery_key: str | None = None,
    ) -> None:
        if discovery_key is not None:
            if self._discovery_receipts is None:
                raise ValueError("RESOURCE_DISCOVERY_RECEIPTS_REQUIRED")
            DiscoverySelectionReceipts._key(discovery_key)
        if artifact_root is not None:
            if not artifact_root.is_absolute():
                raise ValueError("Artifact publication requires an absolute host root")
            artifact_root = artifact_root.resolve()
        if scope.generation_id != self._generation_id:
            raise ValueError("Worker generation does not match broker")
        existing = self._activations.get(scope.activation_id)
        if existing is None:
            if any(item.worker is worker for item in self._activations.values()):
                raise ValueError("A resource worker cannot serve multiple activations")
            self._activations[scope.activation_id] = _Activation(worker, {scope.binding_id: scope})
            if artifact_root is not None:
                self._activations[scope.activation_id].artifact_roots[scope.binding_id] = (
                    artifact_root
                )
            if discovery_key is not None:
                self._activations[scope.activation_id].discovery_keys[scope.binding_id] = (
                    discovery_key
                )
            return
        first = next(iter(existing.scopes.values()))
        if (
            existing.closed
            or existing.worker is not worker
            or first.identity != scope.identity
            or first.build_digest != scope.build_digest
            or first.profile_digest != scope.profile_digest
            or first.binding_snapshot_digest != scope.binding_snapshot_digest
        ):
            raise ValueError("Worker activation identity and Build are immutable")
        if scope.binding_id in existing.scopes and existing.scopes[scope.binding_id] != scope:
            raise ValueError("Worker binding registration is immutable")
        if scope.binding_id in existing.scopes and existing.discovery_keys.get(
            scope.binding_id
        ) != discovery_key:
            raise ValueError("Worker discovery run is immutable")
        if (
            scope.binding_id in existing.scopes
            and existing.artifact_roots.get(scope.binding_id) != artifact_root
        ):
            raise ValueError("Worker artifact publication root is immutable")
        existing.scopes[scope.binding_id] = scope
        if artifact_root is not None:
            existing.artifact_roots[scope.binding_id] = artifact_root
        if discovery_key is not None:
            existing.discovery_keys[scope.binding_id] = discovery_key

    async def dispatch(self, payload: dict) -> dict:
        try:
            frame = encode_frame(payload)
            request = ResourceRequest.model_validate_json(frame[4:])
        except (ValidationError, FrameError):
            return self._error("RESOURCE_REQUEST_INVALID")
        try:
            scope = self._registry.resolve(
                request.handle, operation=request.operation, generation_id=self._generation_id
            )
        except LeaseRejected:
            return self._error("RESOURCE_SCOPE_INVALID", request.request_id)
        activation = self._activations.get(scope.activation_id)
        if (
            activation is None
            or activation.closed
            or activation.scopes.get(scope.binding_id) != scope
        ):
            return self._error("RESOURCE_ACTIVATION_UNAVAILABLE", request.request_id)
        remaining = request.deadline / 1000 - self._clock()
        if remaining <= 0:
            return self._error("RESOURCE_DEADLINE_EXCEEDED", request.request_id)
        if request.check_only:
            if request.arguments:
                return self._error("RESOURCE_ARGUMENTS_INVALID", request.request_id)
            if getattr(activation.worker, "is_running", True) is False:
                return self._error("RESOURCE_ACTIVATION_UNAVAILABLE", request.request_id)
            # Inventory checks prove local lease admission only. They neither
            # call upstream nor claim a write, authorize approval, or renew leases.
            return {"v": 1, "requestId": request.request_id, "result": {"available": True}}
        if request.operation in _WRITES and self._write_authorizer is None:
            return self._error("RESOURCE_APPROVAL_REQUIRED", request.request_id)
        if request.operation == "search_knowledge_base":
            try:
                KnowledgeQuery.model_validate(request.arguments)
            except ValidationError:
                return self._error("RESOURCE_ARGUMENTS_INVALID", request.request_id)
        if request.operation == "load_memory":
            try:
                MemoryQuery.model_validate(request.arguments)
            except ValidationError:
                return self._error("RESOURCE_ARGUMENTS_INVALID", request.request_id)
        if request.operation == "save_memory":
            try:
                parsed = MemoryWrite.model_validate(request.arguments)
                if not parsed.content.strip():
                    raise ValueError("Empty memory content")
            except ValueError:
                return self._error("RESOURCE_ARGUMENTS_INVALID", request.request_id)
        if request.operation == "memory_status":
            try:
                MemoryStatusQuery.model_validate(request.arguments)
            except ValueError:
                return self._error("RESOURCE_ARGUMENTS_INVALID", request.request_id)
        if request.operation in {"update_memory", "delete_memory"}:
            try:
                if request.operation == "update_memory":
                    if not MemoryUpdate.model_validate(request.arguments).content.strip():
                        raise ValueError("Empty memory content")
                else:
                    MemoryRecordSelector.model_validate(request.arguments)
            except ValueError:
                return self._error("RESOURCE_ARGUMENTS_INVALID", request.request_id)
        if request.operation == "execute_skills":
            try:
                query = SkillExecutionQuery.model_validate(request.arguments)
                if not query.workflow_prompt.strip():
                    raise ValueError("Empty workflow")
            except ValueError:
                return self._error("RESOURCE_ARGUMENTS_INVALID", request.request_id)
            if scope.binding_id not in activation.artifact_roots:
                return self._error("RESOURCE_ARTIFACT_DELIVERY_REQUIRED", request.request_id)
        if request.operation == "read_skill_artifact":
            try:
                SkillArtifactQuery.model_validate(request.arguments)
            except ValueError:
                return self._error("RESOURCE_ARGUMENTS_INVALID", request.request_id)
            if self._skill_artifacts is None:
                return self._error("RESOURCE_OPERATION_NOT_FOUND", request.request_id)
        # The ingress cannot extend worker execution indefinitely with a distant
        # deadline. Bound both the wait and the deadline sent into the subprocess.
        remaining = min(remaining, 900.0 if request.operation == "execute_skills" else 60.0)
        request = request.model_copy(
            update={"deadline": min(request.deadline, int((self._clock() + remaining) * 1000))}
        )
        if len(activation.tasks) >= self._max_inflight:
            return self._error("RESOURCE_BUSY", request.request_id)
        # No await between admission and recording the task. Recheck immediately
        # before the worker call so a queued task cannot bypass revocation.
        task = asyncio.create_task(self._invoke(activation, request, scope))
        activation.tasks.add(task)
        task.add_done_callback(activation.tasks.discard)
        # A timeout or disconnected caller must not cancel a possibly sent write.
        # Only the lifecycle owner cancels these tasks after revocation and Worker
        # shutdown. wait() distinguishes that child cancellation from cancellation
        # of this caller, which must still propagate to the transport/engine.
        done, _ = await asyncio.wait((task,), timeout=remaining)
        if not done:
            if request.operation in _WRITES:
                return self._error("RESOURCE_WRITE_UNKNOWN", request.request_id)
            return self._error("RESOURCE_DEADLINE_EXCEEDED", request.request_id)
        if task.cancelled():
            if not activation.closed:
                raise asyncio.CancelledError
            code = (
                "RESOURCE_WRITE_UNKNOWN" if request.operation in _WRITES
                else "RESOURCE_ACTIVATION_UNAVAILABLE"
            )
            return self._error(code, request.request_id)
        return task.result()

    async def _invoke(
        self, activation: _Activation, request: ResourceRequest, scope: ResourceScope
    ) -> dict:
        # Keep the serial SDK queue here so admission is checked after waiting,
        # immediately before handing the request to the worker transport.
        async with activation.execution_lock:
            return await self._invoke_admitted(activation, request, scope)

    async def _invoke_admitted(
        self, activation: _Activation, request: ResourceRequest, scope: ResourceScope
    ) -> dict:
        try:
            self._registry.authorize(
                request.handle,
                operation=request.operation,
                generation_id=self._generation_id,
                activation_id=scope.activation_id,
            )
            if activation.closed or request.deadline / 1000 <= self._clock():
                return self._error("RESOURCE_DEADLINE_EXCEEDED", request.request_id)
            if request.operation == "read_skill_artifact":
                try:
                    result = await asyncio.to_thread(
                        self._skill_artifacts.read,
                        request.arguments,
                        authority_digest=self._authority(scope),
                    )
                except Exception:
                    return self._error("RESOURCE_ARTIFACT_NOT_FOUND", request.request_id)
                self._registry.authorize(
                    request.handle,
                    operation=request.operation,
                    generation_id=self._generation_id,
                    activation_id=scope.activation_id,
                )
                if request.deadline / 1000 <= self._clock():
                    return self._error("RESOURCE_DEADLINE_EXCEEDED", request.request_id)
                return {"v": 1, "requestId": request.request_id, "result": result}
            receipt = None
            status_receipt = None
            execution_selections = None
            if (
                request.operation == "execute_skills"
                and scope.binding_id in activation.discovery_keys
            ):
                selections = await asyncio.to_thread(
                    self._discovery_receipts.read, activation.discovery_keys[scope.binding_id]
                )
                self._registry.authorize(
                    request.handle, operation=request.operation,
                    generation_id=self._generation_id, activation_id=scope.activation_id,
                )
                selected = {item.skill_id: item for item in selections}
                query = SkillExecutionQuery.model_validate(request.arguments)
                if any(skill_id not in selected for skill_id in query.skill_ids):
                    return self._error("RESOURCE_SKILL_SELECTION_REQUIRED", request.request_id)
                execution_selections = [
                    selected[skill_id].model_dump(by_alias=True, mode="json")
                    for skill_id in query.skill_ids
                ]
            if request.operation == "memory_status":
                if self._operation_ledger is None:
                    return self._error("RESOURCE_OPERATION_NOT_FOUND", request.request_id)
                query = MemoryStatusQuery.model_validate(request.arguments)
                status_receipt = await asyncio.to_thread(
                    self._operation_ledger.lookup,
                    query.operation_id,
                    authority_digest=self._authority(scope),
                    operation="save_memory",
                )
                if status_receipt is None:
                    return self._error("RESOURCE_OPERATION_NOT_FOUND", request.request_id)
                self._registry.authorize(
                    request.handle,
                    operation=request.operation,
                    generation_id=self._generation_id,
                    activation_id=scope.activation_id,
                )
                if activation.closed or request.deadline / 1000 <= self._clock():
                    return self._error("RESOURCE_DEADLINE_EXCEEDED", request.request_id)
            if request.operation in _WRITES:
                assert self._write_authorizer is not None and self._operation_ledger is not None
                event_id = await self._write_authorizer.authorize(request, scope)
                if not event_id:
                    return self._error("RESOURCE_APPROVAL_REQUIRED", request.request_id)
                authority = self._authority(scope)
                receipt = await asyncio.to_thread(
                    self._operation_ledger.claim,
                    authority_digest=authority,
                    event_id=event_id,
                    operation=request.operation,
                    arguments=(
                        {**request.arguments, "_hostSelections": execution_selections}
                        if execution_selections is not None else request.arguments
                    ),
                )
                # Authorization may have changed while awaiting approval or disk.
                self._registry.authorize(
                    request.handle,
                    operation=request.operation,
                    generation_id=self._generation_id,
                    activation_id=scope.activation_id,
                )
                if activation.closed or request.deadline / 1000 <= self._clock():
                    return self._error("RESOURCE_WRITE_UNKNOWN", request.request_id)
                if not receipt.claimed:
                    previous = None
                    if request.operation == "execute_skills" and self._skill_artifacts is not None:
                        previous = await asyncio.to_thread(
                            self._skill_artifacts.result,
                            receipt.operation_id,
                            authority_digest=authority,
                        )
                        self._registry.authorize(
                            request.handle,
                            operation=request.operation,
                            generation_id=self._generation_id,
                            activation_id=scope.activation_id,
                        )
                    elif request.operation in {"update_memory", "delete_memory"}:
                        previous = await asyncio.to_thread(
                            self._operation_ledger.memory_mutation_result,
                            receipt.operation_id,
                            authority_digest=authority,
                            operation=request.operation,
                        )
                    self._registry.authorize(
                        request.handle,
                        operation=request.operation,
                        generation_id=self._generation_id,
                        activation_id=scope.activation_id,
                    )
                    if activation.closed or request.deadline / 1000 <= self._clock():
                        return self._error("RESOURCE_DEADLINE_EXCEEDED", request.request_id)
                    if previous is not None:
                        return {
                            "v": 1,
                            "requestId": request.request_id,
                            "result": {**previous, "replayed": False},
                        }
                    return {
                        "v": 1,
                        "requestId": request.request_id,
                        "result": {
                            "status": receipt.state,
                            "operationId": receipt.operation_id,
                            "replayed": False,
                        },
                    }
            worker_request = (
                request
                if receipt is None
                else request.model_copy(update={"request_id": receipt.operation_id})
            )
            if execution_selections is not None:
                worker_request = worker_request.model_copy(update={"arguments": {
                    **request.arguments, "_hostSelections": execution_selections,
                }})
            result = await activation.worker.invoke(worker_request, scope)
            selection = result.pop("_discoverySelection", None)
            if selection is not None:
                key = activation.discovery_keys.get(scope.binding_id)
                if (
                    key is None or self._discovery_receipts is None
                    or request.operation not in {"load_skill", "read_skill_resource"}
                    or result.get("status") != "ok"
                ):
                    raise ValueError("Unexpected discovery selection")
                selected = DiscoverySkillReceipt.model_validate(selection)
                if (
                    selected.skill_id != result.get("skillId")
                    or selected.version_id != result.get("versionId")
                    or selected.content_hash != result.get("contentHash")
                    or selected.skill_id != request.arguments.get(
                        "skillId", request.arguments.get("skill_id")
                    )
                ):
                    raise ValueError("Discovery selection does not match request/result")
                self._registry.authorize(
                    request.handle, operation=request.operation,
                    generation_id=self._generation_id, activation_id=scope.activation_id,
                )
                await asyncio.to_thread(self._discovery_receipts.record, key, selected)
            elif (
                scope.binding_id in activation.discovery_keys
                and request.operation in {"load_skill", "read_skill_resource"}
                and result.get("status") == "ok"
            ):
                raise ValueError("Discovery selection receipt is missing")
            if (
                result.get("status") in {"failed", "unauthorized"}
                and result.get("errorCode", result.get("error_code")) == "RESOURCE_FORBIDDEN"
            ):
                self._registry.revoke_binding(scope.activation_id, scope.binding_id)
                denied = self._error("RESOURCE_FORBIDDEN", request.request_id)
                if receipt is not None:
                    # Authentication failure is not a confirmed business write
                    # receipt. Preserve the claimed operation for reconciliation.
                    denied["operationReceipt"] = {
                        "operationId": receipt.operation_id, "state": receipt.state,
                    }
                return denied
            if receipt is not None and request.operation in {"update_memory", "delete_memory"}:
                mutation = MemoryMutationReceipt.model_validate(result)
                if (
                    mutation.operation_id != receipt.operation_id
                    or mutation.memory_id
                    != request.arguments.get("memoryId", request.arguments.get("memory_id"))
                ):
                    raise ValueError("Invalid memory mutation receipt")
                result = mutation.model_dump(by_alias=True, mode="json")
                if mutation.status in {"succeeded", "failed"}:
                    receipt = await asyncio.to_thread(
                        self._operation_ledger.record_memory_mutation,
                        mutation,
                        authority_digest=self._authority(scope),
                        operation=request.operation,
                    )
            if receipt is not None and request.operation == "execute_skills":
                # Never forward the private Worker result, including on malformed
                # receipts. Only host-published metadata can reach the tool caller.
                if (
                    set(result) != {"operationId", "status", "outputFiles"}
                    or result["operationId"] != receipt.operation_id
                    or result["status"] not in {"succeeded", "failed", "unknown"}
                    or not isinstance(result["outputFiles"], list)
                    or any(not isinstance(path, str) for path in result["outputFiles"])
                ):
                    raise ValueError("Invalid Skill execution receipt")
                if result["status"] in {"succeeded", "failed"}:
                    assert self._skill_artifacts is not None
                    result = await asyncio.to_thread(
                        self._skill_artifacts.publish,
                        receipt.operation_id,
                        authority_digest=self._authority(scope),
                        root=activation.artifact_roots[scope.binding_id],
                        paths=result["outputFiles"],
                        status=result["status"],
                    )
                    receipt = await asyncio.to_thread(
                        self._operation_ledger.lookup,
                        receipt.operation_id,
                        authority_digest=self._authority(scope),
                        operation="execute_skills",
                    )
                else:
                    result = {
                        "status": "unknown",
                        "operationId": receipt.operation_id,
                        "artifacts": [],
                    }
            if status_receipt is not None:
                if result == {
                    "operationId": status_receipt.operation_id,
                    "extractionStatus": "failed",
                    "searchable": False,
                    "errorCode": None,
                } and status_receipt.state in {"unknown", "accepted_pending", "failed"}:
                    status_receipt = await asyncio.to_thread(
                        self._operation_ledger.record, status_receipt.operation_id, "failed"
                    )
                result = {**result, "status": status_receipt.state}
            if (
                receipt is not None
                and request.operation == "save_memory"
                and result
                == {
                    "status": "accepted_pending",
                    "operationId": receipt.operation_id,
                    "searchable": False,
                    "errorCode": None,
                }
            ):
                # Fixed memory submission receipt from our initialized Worker;
                # acceptance is not a searchable/succeeded terminal state.
                receipt = await asyncio.to_thread(
                    self._operation_ledger.record, receipt.operation_id, "accepted_pending"
                )
            self._registry.authorize(
                request.handle,
                operation=request.operation,
                generation_id=self._generation_id,
                activation_id=scope.activation_id,
            )
            reply = {"v": 1, "requestId": request.request_id, "result": result}
            if receipt is not None:
                # The trusted business adapter must reconcile authoritative status;
                # arbitrary Worker text cannot mark a durable operation successful.
                reply["operationReceipt"] = {
                    "operationId": receipt.operation_id,
                    "state": receipt.state,
                }
            encode_frame(reply)
            return reply
        except LeaseRejected:
            return self._error("RESOURCE_SCOPE_INVALID", request.request_id)
        except OperationConflict:
            return self._error("RESOURCE_OPERATION_CONFLICT", request.request_id)
        except Exception:
            if request.operation in _WRITES:
                return self._error("RESOURCE_WRITE_UNKNOWN", request.request_id)
            return self._error("RESOURCE_WORKER_FAILED", request.request_id)

    @staticmethod
    def _authority(scope: ResourceScope) -> str:
        identity = scope.identity.model_dump(mode="json", exclude={"session_ref"})
        return hashlib.sha256(
            json.dumps(
                [identity, scope.binding_id, scope.binding_snapshot_digest],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def revoke_activation(self, activation_id: str) -> None:
        self._registry.revoke_activation(activation_id)
        activation = self._activations.get(activation_id)
        if activation is not None:
            activation.closed = True

    def revoke_generation(self) -> None:
        self._registry.revoke_generation(self._generation_id)
        for activation in self._activations.values():
            activation.closed = True

    async def retire_activation(self, activation_id: str) -> None:
        """Owner closes the worker first, then drains and removes broker state."""
        self.revoke_activation(activation_id)
        activation = self._activations.get(activation_id)
        if activation is None:
            return
        tasks = tuple(activation.tasks)
        if tasks:
            # The owner has closed the Worker. Approval waiters and queued calls
            # must not retain the supervisor lock until a UI response or deadline.
            # Existing ledger claims remain unknown; cancellation is not rollback.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._activations.get(activation_id) is activation:
            del self._activations[activation_id]

    @staticmethod
    def _error(code: str, request_id: str = "") -> dict:
        return {"v": 1, "requestId": request_id, "error": {"code": code}}
