"""Resource write authorization from the existing local Kernel interaction ledger."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone

from ksadk.interaction.contracts import InteractionPresentation, InteractionRecord
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
from ksadk.resource_runtime.ipc import ResourceRequest
from ksadk.resource_runtime.leases import ResourceScope
from ksadk.resource_runtime.memory_provider import MemoryRecordSelector, MemoryUpdate, MemoryWrite
from ksadk.resource_runtime.skills import SkillExecutionQuery


def approval_metadata(request: ResourceRequest, scope: ResourceScope, event_id: str) -> dict:
    """Host-only metadata for an InteractionRecord, before presenting approval.

    No body or credentials are persisted here. Activation/transport IDs may change
    on recovery; the bound identity, Build, operation and exact arguments may not.
    """
    payload = {
        "identity": scope.identity.model_dump(mode="json"),
        "build": scope.build_digest,
        "binding": scope.binding_id,
        "snapshot": scope.binding_snapshot_digest,
        "operation": request.operation,
        "arguments": request.arguments,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {"resourceApproval": {"version": 1, "eventId": event_id, "requestDigest": digest}}


class KernelResourceWriteAuthorizer:
    """Use the existing Kernel ledger; decisions remain owned by its ingress.

    resolve_interaction maps a trusted runtime tool event to its interaction ID.
    Never implement that mapping from model arguments or browser approval flags.
    PostgreSQL/remote hosts need an equivalent committed-receipt adapter.
    """

    def __init__(
        self,
        store: SQLiteAgentKernelStore,
        *,
        run_id: str,
        resolve_interaction: Callable[[ResourceRequest, ResourceScope], str | None],
    ):
        self.store = store
        self.run_id = run_id
        self.resolve_interaction = resolve_interaction

    async def request_memory_approval(
        self,
        request: ResourceRequest,
        scope: ResourceScope,
        *,
        interaction_id: str,
        event_id: str,
        guard: ActivationWriteGuard,
    ) -> InteractionRecord:
        if request.operation != "save_memory":
            raise ValueError("RESOURCE_SCOPE_DENIED")
        return await self.request_write_approval(
            request,
            scope,
            interaction_id=interaction_id,
            event_id=event_id,
            guard=guard,
        )

    async def request_write_approval(
        self,
        request: ResourceRequest,
        scope: ResourceScope,
        *,
        interaction_id: str,
        event_id: str,
        guard: ActivationWriteGuard,
    ) -> InteractionRecord:
        """Host-only creation, fenced by the existing Kernel activation lease.

        Presentation contains the proposed operation so users can review the actual
        write. It follows the host's existing interaction/transcript retention.
        This method cannot submit an approval decision or execute a write.
        """
        scope = ResourceScope.model_validate(scope.model_dump())
        request = ResourceRequest.model_validate_json(request.model_dump_json())
        if request.operation not in scope.allowed_operations:
            raise ValueError("RESOURCE_SCOPE_DENIED")
        if request.operation == "save_memory":
            content = MemoryWrite.model_validate(request.arguments).content
            title = "保存长期记忆"
        elif request.operation == "update_memory":
            update = MemoryUpdate.model_validate(request.arguments)
            if not update.content.strip():
                raise ValueError("RESOURCE_APPROVAL_REQUEST_INVALID")
            content = update.memory_id + "\n\n" + update.content
            title = "更新长期记忆"
        elif request.operation == "delete_memory":
            content = MemoryRecordSelector.model_validate(request.arguments).memory_id
            title = "删除长期记忆（软删除）"
        elif request.operation == "execute_skills":
            execution = SkillExecutionQuery.model_validate(request.arguments)
            if not execution.workflow_prompt.strip():
                raise ValueError("RESOURCE_APPROVAL_REQUEST_INVALID")
            content = ", ".join(execution.skill_ids) + "\n\n" + execution.workflow_prompt
            title = "执行锁定 Skill"
        else:
            raise ValueError("RESOURCE_APPROVAL_OPERATION_UNSUPPORTED")
        if not content.strip() or not isinstance(event_id, str) or not 0 < len(event_id) <= 1024:
            raise ValueError("RESOURCE_APPROVAL_REQUEST_INVALID")
        record = InteractionRecord(
            interaction_id=interaction_id,
            tenant_id=scope.identity.tenant_ref,
            agent_instance_id=scope.identity.agent_id,
            session_id=scope.identity.session_ref,
            run_id=self.run_id,
            kind="approval",
            request_schema={"type": "object", "properties": {}, "additionalProperties": False},
            created_at=datetime.now(timezone.utc).isoformat(),
            provider_id="platform-resources",
            presentation=InteractionPresentation(
                title=title,
                description=content,
            ),
            continuation_metadata=approval_metadata(request, scope, event_id),
        )
        return await self.store.request(record, guard=guard)

    async def authorize(self, request: ResourceRequest, scope: ResourceScope) -> str | None:
        interaction_id = self.resolve_interaction(request, scope)
        if not interaction_id:
            return None
        lookup = {
            "tenant_id": scope.identity.tenant_ref,
            "agent_instance_id": scope.identity.agent_id,
            "session_id": scope.identity.session_ref,
            "run_id": self.run_id,
        }
        record = await self.store.get(interaction_id, **lookup)
        if record is None or record.kind != "approval" or record.status != "resolved":
            return None
        receipt = await self.store.get_terminal_receipt(interaction_id, **lookup)
        if (
            receipt is None
            or receipt.outcome != "approved"
            or receipt.revision != record.revision
            or not receipt.event_id
        ):
            return None
        metadata = (record.continuation_metadata or {}).get("resourceApproval")
        if not isinstance(metadata, dict):
            return None
        event_id = metadata.get("eventId")
        if not isinstance(event_id, str) or not 0 < len(event_id) <= 1024:
            return None
        if metadata != approval_metadata(request, scope, event_id)["resourceApproval"]:
            return None
        return event_id
