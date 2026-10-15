"""Scoped, paged lookup of an already accounted direct child's original result.

The read capability owns no Kernel, model, factory, lease or execution method.
An accounted identity is a locator, never a semantic completion verdict.
"""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import Field

from ksadk.harness.native.ports import ToolDefinition, ToolRequest, ToolResult
from ksadk.harness.native.state import Contract, RunScope, RunSnapshot
from ksadk.harness.native.subagents import ChildRole
from ksadk.harness.stores.native_contract import RunStore, ScopeMismatch, StoreConflict


class _ResultQuery(Contract):
    child_run_id: str = Field(min_length=1, max_length=256, pattern=r"\S")
    offset_chars: int = Field(default=0, strict=True, ge=0)
    limit_chars: int = Field(default=4000, strict=True, ge=1, le=16000)


class NativeChildResultPort:
    def __init__(self, *, scope: RunScope, store: RunStore, roles: tuple[ChildRole, ...]):
        self.scope = RunScope.model_validate_json(scope.model_dump_json())
        self.store = store
        self.roles = tuple(ChildRole.model_validate_json(role.model_dump_json()) for role in roles)
        if not self.roles or len({role.role_id for role in self.roles}) != len(self.roles):
            raise ValueError("unique locked child roles are required")

    def definition(self) -> ToolDefinition:
        role_identity = json.dumps(
            [role.model_dump(mode="json") for role in self.roles],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return ToolDefinition(
            name="native_child_result",
            description=(
                "Read a page of an already accounted child Run's original delivery. "
                "This query starts no task and performs no model replay; its result "
                "does not verify the parent task."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["child_run_id"],
                "properties": {
                    "child_run_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "offset_chars": {"type": "integer", "minimum": 0, "default": 0},
                    "limit_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 16000,
                        "default": 4000,
                    },
                },
            },
            read_only=True,
            concurrent_safe=True,
            feedback_profile_digest="sha256:"
            + hashlib.sha256(
                ("native.child-result/v2\0" + role_identity).encode("utf-8")
            ).hexdigest(),
        )

    @staticmethod
    def _error(code: str) -> ToolResult:
        return ToolResult(status="error", content={"error_code": code})

    def _load_direct_child(self, parent: RunSnapshot, child_run_id: str) -> RunSnapshot | None:
        if child_run_id not in parent.child_run_ids:
            return None
        # Scope is compiled from the current locked role, never accepted in
        # model arguments. The complete parent identity must match the origin.
        for role in self.roles:
            child_scope = self.scope.model_copy(
                update={"agent_id": role.agent_id or self.scope.agent_id}
            )
            try:
                child = self.store.load(child_scope, child_run_id)
            except (KeyError, ScopeMismatch):
                # Probe only scopes compiled from the current locked roles.
                # A nonmatching role is not a store outage; try the next grant.
                continue
            origin = child.origin
            if (
                child.scope == child_scope
                and origin.get("kind") == "native_subagent"
                and origin.get("parent_run_id") == parent.run_id
                and origin.get("parent_scope") == self.scope.model_dump(mode="json")
                and origin.get("role") == role.role_id
                and isinstance(origin.get("parent_operation_key"), str)
                and isinstance(origin.get("parent_tool_call_id"), str)
                and self._bound_to_parent_receipt(parent, child)
            ):
                return child
        return None

    def _load_session_child(
        self, parent: RunSnapshot, child_run_id: str,
    ) -> tuple[RunSnapshot, RunSnapshot] | None:
        """Follow only this parent's committed Session ancestry and receipts.

        A later user turn has a new Run and an activation can have a new owner.
        Neither requires a surviving process handle for an accounted terminal
        child. Ordinary result lookup remains restricted to direct children.
        """
        cursor = parent
        while True:
            child = self._load_direct_child(cursor, child_run_id)
            if child is not None:
                return child, cursor
            if cursor.parent_run_id is None:
                return None
            try:
                previous = self.store.load(self.scope, cursor.parent_run_id)
            except KeyError:
                return None
            if previous.session_id != parent.session_id or not previous.phase.terminal:
                return None
            cursor = previous

    def _bound_to_parent_receipt(self, parent: RunSnapshot, child: RunSnapshot) -> bool:
        operation = child.origin["parent_operation_key"]
        receipt = self.store.get_receipt(self.scope, parent.run_id, operation)
        if receipt is None or receipt.operation_key != operation:
            return False
        if receipt.status not in {"committed", "uncertain"}:
            return False
        try:
            result = ToolResult.model_validate(receipt.result)
        except ValueError:
            return False
        reservation = result.child_budget_reservation
        if reservation is not None:
            admitted = next((item for item in parent.child_budget_reservations
                             if item.child_run_id == child.run_id), None)
            if (
                admitted is None or admitted.parent_operation_key != operation
                or reservation.child_run_id != child.run_id
                or reservation.child_scope != child.scope
                or reservation.child_request_digest != child.origin.get("child_request_digest")
                or admitted.child_request_digest != reservation.child_request_digest
                or admitted.configuration_digest != child.configuration_digest
            ):
                return False
        elif child.run_id not in result.resource_usage.child_run_ids:
            return False
        intent = self.store.get_intent(self.scope, parent.run_id, operation)
        call = intent.payload.get("call") if intent is not None and intent.kind == "tool" else None
        if intent is None and (member := re.fullmatch(r"(.+):call:([1-9][0-9]*)", operation)):
            intent = self.store.get_intent(self.scope, parent.run_id, member[1])
            calls = intent.payload.get("calls") if intent is not None else None
            index = int(member[2]) - 1
            if (
                intent is not None
                and intent.kind == "tool"
                and intent.payload.get("parallel_delegation") is True
                and isinstance(calls, list)
            ):
                call = calls[index] if index < len(calls) else None
        if not isinstance(call, dict):
            return False
        args = call.get("arguments")
        return (
            call.get("name") in (
                {"native_start_child", "native_delegate"} if reservation is not None
                else {"native_delegate"}
            )
            and call.get("call_id") == child.origin["parent_tool_call_id"]
            and isinstance(args, dict)
            and args.get("role") == child.origin["role"]
            and isinstance(args.get("task"), str)
            and child.matches_initial_goal(args["task"])
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        if (
            request.scope != self.scope
            or request.definition != self.definition()
            or request.call.name != self.definition().name
        ):
            return self._error("child_result_query_not_authorized")
        try:
            query = _ResultQuery.model_validate(request.call.arguments)
        except ValueError:
            return self._error("child_result_query_invalid")
        try:
            parent = self.store.load(self.scope, request.run_id)
            child = self._load_direct_child(parent, query.child_run_id)
        except KeyError:
            child = None
        except StoreConflict:
            return ToolResult(
                status="uncertain", content={"error_code": "child_result_unavailable"}
            )
        if child is None:
            # Do not reveal whether a foreign or unauthorized Run exists.
            return self._error("child_result_not_available")
        if query.offset_chars > len(child.final_text):
            return self._error("child_result_offset_invalid")
        final_text = child.final_text[query.offset_chars : query.offset_chars + query.limit_chars]
        stop = query.offset_chars + len(final_text)
        return ToolResult(
            status="ok",
            content={
                "schema_version": "native.child-result/v1",
                "child_run_id": child.run_id,
                "child_session_id": child.session_id,
                "parent_run_id": parent.run_id,
                "parent_tool_call_id": child.origin["parent_tool_call_id"],
                "parent_operation_key": child.origin["parent_operation_key"],
                "role": child.origin["role"],
                "snapshot_revision": child.revision,
                "event_seq": child.event_seq,
                "goal_digest": hashlib.sha256(child.goal.encode("utf-8")).hexdigest(),
                "contract_digest": child.contract_digest,
                "phase": child.phase.value,
                "terminal": child.phase.terminal,
                "outcome": child.outcome,
                "final_text": final_text,
                "final_text_digest": hashlib.sha256(child.final_text.encode("utf-8")).hexdigest(),
                "offset_chars": query.offset_chars,
                "next_offset_chars": stop,
                "complete": stop == len(child.final_text),
                "total_chars": len(child.final_text),
                "parent_acceptance": "not_evaluated",
                "execution_started": False,
                "resource_usage_accounted_again": False,
            },
        )

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        # This is the same scoped read, with no execution fallback.
        return await self.execute(request)


class _TasksQuery(Contract):
    offset: int = Field(default=0, strict=True, ge=0)
    limit: int = Field(default=8, strict=True, ge=1, le=16)
    inventory_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class NativeChildTasksPort:
    """Enumerate the parent's accounted inventory using the result grant.

    The inventory cursor binds identities, not an atomic snapshot of all child
    states. Each entry reports its own current revision. Task prose stays tool
    data; status and delivery availability never grant semantic acceptance.
    """

    def __init__(self, *, result_port: NativeChildResultPort):
        self.result_port = result_port

    def _direct_inventory(self, parent: RunSnapshot) -> tuple[RunSnapshot, ...] | None:
        """Project direct relations without deleting descendant accounting.

        Only authenticated direct child snapshots may identify their own
        descendants. An unavailable or revoked direct child still fails closed;
        arbitrary tool prose cannot turn a missing Run into an excluded leaf.
        """
        children = []
        descendants = set()
        for child_id in parent.child_run_ids:
            child = self.result_port._load_direct_child(parent, child_id)
            if child is not None:
                children.append(child)
                descendants.update(child.child_run_ids)
        direct_ids = {child.run_id for child in children}
        if any(child_id not in direct_ids and child_id not in descendants
               for child_id in parent.child_run_ids):
            return None
        return tuple(children)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="native_child_tasks",
            description=(
                "List saved direct child tasks, their current status and delivery locators. "
                "Use this when previous child results or IDs are no longer in context. "
                "For later pages, use the returned inventory_digest and next_offset. "
                "Starts no work. Read a delivery with native_child_result."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8},
                    "inventory_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
            read_only=True,
            concurrent_safe=True,
            feedback_profile_digest="sha256:"
            + hashlib.sha256(
                (
                    "native.child-tasks/v2\0direct-child-inventory/v2\0"
                    + self.result_port.definition().feedback_profile_digest
                ).encode()
            ).hexdigest(),
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        reader = self.result_port
        if (
            request.scope != reader.scope
            or request.definition != self.definition()
            or request.call.name != self.definition().name
        ):
            return reader._error("child_tasks_query_not_authorized")
        try:
            query = _TasksQuery.model_validate(request.call.arguments)
        except ValueError:
            return reader._error("child_tasks_query_invalid")
        try:
            parent = reader.store.load(reader.scope, request.run_id)
            children = self._direct_inventory(parent)
            if children is None:
                return reader._error("child_tasks_not_available")
            identity = json.dumps(
                {
                    "inventory_policy": "direct-child-inventory/v2",
                    "scope": reader.scope.model_dump(mode="json"),
                    "parent_run_id": parent.run_id,
                    "child_run_ids": tuple(child.run_id for child in children),
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            inventory_digest = hashlib.sha256(identity.encode()).hexdigest()
            if (query.offset and query.inventory_digest is None) or query.inventory_digest not in (
                None,
                inventory_digest,
            ):
                return reader._error("child_tasks_inventory_changed")
            if query.offset > len(children):
                return reader._error("child_tasks_offset_invalid")
            tasks = []
            for child in children[query.offset : query.offset + query.limit]:
                tasks.append(
                    {
                        "child_run_id": child.run_id,
                        "child_session_id": child.session_id,
                        "role": child.origin["role"],
                        "parent_tool_call_id": child.origin["parent_tool_call_id"],
                        "snapshot_revision": child.revision,
                        "phase": child.phase.value,
                        "terminal": child.phase.terminal,
                        "outcome": child.outcome,
                        "goal_preview": child.goal[:256],
                        "goal_preview_trust": "untrusted",
                        "goal_preview_complete": len(child.goal) <= 256,
                        "goal_digest": hashlib.sha256(child.goal.encode()).hexdigest(),
                        "final_text_chars": len(child.final_text),
                        "final_text_digest": hashlib.sha256(child.final_text.encode()).hexdigest(),
                        "result_tool": "native_child_result",
                        "parent_acceptance": "not_evaluated",
                    }
                )
        except KeyError:
            return reader._error("child_tasks_not_available")
        except StoreConflict:
            return ToolResult(status="uncertain", content={"error_code": "child_tasks_unavailable"})
        stop = query.offset + len(tasks)
        return ToolResult(
            status="ok",
            content={
                "schema_version": "native.child-tasks/v2",
                "parent_run_id": parent.run_id,
                "parent_snapshot_revision": parent.revision,
                "inventory_digest": inventory_digest,
                "tasks": tasks,
                "total_tasks": len(children),
                "offset": query.offset,
                "next_offset": stop,
                "complete": stop == len(children),
                "parent_acceptance": "not_evaluated",
                "execution_started": False,
                "resource_usage_accounted_again": False,
            },
        )

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        return await self.execute(request)
