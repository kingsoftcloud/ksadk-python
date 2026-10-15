"""Bind a Native tool approval to the current Studio Run and exact tool call."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from ksadk.harness.native.ports import ToolRequest, ToolResult
from ksadk.harness.native.state import RunScope
from ksadk.harness.stores.native_contract import RunStore


class StudioNativeApprovalAuthorizer:
    """Validate each execution against the host-owned pending call and grant."""

    def __init__(self, *, scope: RunScope, store: RunStore, full_access: bool) -> None:
        self._scope = scope
        self._store = store
        self._full_access = full_access
        self._approval_required: set[str] = set()

    def require_approval(self, names: Iterable[str]) -> None:
        self._approval_required.update(names)

    async def __call__(self, request: ToolRequest) -> str | ToolResult:
        if request.scope != self._scope:
            return "deny"
        try:
            snapshot = self._store.load(self._scope, request.run_id)
        except Exception:
            return "deny"
        ledger_call = request.admission_call or request.call
        if request.admission_call is not None and (
            ledger_call.name != "mcp_call_tool"
            or ledger_call.call_id != request.call.call_id
            or ledger_call.arguments.get("tool_name") != request.call.name
            or ledger_call.arguments.get("arguments") != request.call.arguments
            or request.definition.name != request.call.name
        ):
            return "deny"
        pending = snapshot.pending_tools[0] if snapshot.pending_tools else None
        parent_key = snapshot.pending_intent
        if not parent_key:
            return "deny"
        try:
            intent = self._store.get_intent(self._scope, request.run_id, parent_key)
        except Exception:
            return "deny"
        if intent is None or intent.kind != "tool" or intent.status != "running":
            return "deny"
        if request.operation_key == parent_key:
            if intent.payload.get("call") != pending:
                return "deny"
        else:
            prefix = f"{parent_key}:call:"
            suffix = request.operation_key.removeprefix(prefix)
            if (
                not request.operation_key.startswith(prefix)
                or not suffix.isascii()
                or not suffix.isdecimal()
                or len(suffix) > 4
            ):
                return "deny"
            index = int(suffix)
            calls = intent.payload.get("calls")
            if (
                intent.payload.get("parallel_readonly") is not True
                or not isinstance(calls, list)
                or not 1 <= index <= len(calls)
                or request.operation_key != f"{parent_key}:call:{index}"
                or tuple(calls) != snapshot.pending_tools[: len(calls)]
                or not request.definition.read_only
                or not request.definition.concurrent_safe
                or request.call.name in self._approval_required
                or request.interaction is not None
            ):
                return "deny"
            pending = calls[index - 1]
        if (
            snapshot.scope != self._scope
            or pending is None
            or pending["call_id"] != ledger_call.call_id
            or pending["name"] != ledger_call.name
            or pending["arguments"] != ledger_call.arguments
            or snapshot.interaction != request.interaction
        ):
            return "deny"
        approval_name = (
            ledger_call.arguments.get("tool_name", request.call.name)
            if ledger_call.name == "mcp_call_tool"
            else request.call.name
        )
        if self._full_access or not {
            request.call.name,
            ledger_call.name,
            approval_name,
        }.intersection(self._approval_required):
            return "allow"
        identity = (
            f"{self._scope.tenant_id}\0{self._scope.workspace_id}\0"
            f"{self._scope.user_id}\0{self._scope.agent_id}\0{request.run_id}\0"
            f"{request.call.call_id}\0{approval_name}"
        )
        interaction_id = "native-tool-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        if request.interaction is None:
            return ToolResult(status="approval_required", interaction_id=interaction_id)
        return (
            "allow"
            if request.interaction == {"interaction_id": interaction_id, "decision": "allow"}
            else "deny"
        )
