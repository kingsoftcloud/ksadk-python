"""Request-scoped host authorization for resource writes."""

from __future__ import annotations

import hashlib
import json

from ksadk.resource_runtime.ipc import ResourceRequest
from ksadk.resource_runtime.leases import ResourceScope

_WRITES = frozenset({"save_memory", "update_memory", "delete_memory", "execute_skills"})


class FullAccessResourceWriteAuthorizer:
    """Authorize exact writes covered by a trusted full-access invocation.

    The Studio/API ingress chooses the approval profile. Capability plugins only
    receive this authorizer after the trusted host has resolved that profile for
    the current activation. The returned event reference binds the decision to
    the exact scope and arguments so OperationLedger can fence retries.
    """

    def __init__(self, *, activation_key: str) -> None:
        if not activation_key or any(character.isspace() for character in activation_key):
            raise ValueError("RESOURCE_APPROVAL_ACTIVATION_INVALID")
        self._activation_key = activation_key

    async def authorize(self, request: ResourceRequest, scope: ResourceScope) -> str | None:
        if request.operation not in _WRITES or request.operation not in scope.allowed_operations:
            return None
        payload = {
            "version": 1,
            "approvalProfile": "full",
            "activationKey": self._activation_key,
            "identity": scope.identity.model_dump(mode="json"),
            "build": scope.build_digest,
            "binding": scope.binding_id,
            "snapshot": scope.binding_snapshot_digest,
            "operation": request.operation,
            "arguments": request.arguments,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        return f"studio-full-access:{digest}"


__all__ = ["FullAccessResourceWriteAuthorizer"]
