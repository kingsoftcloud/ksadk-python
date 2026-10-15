"""Bind local child file isolation and controlled merge to Studio's original host."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from ksadk.harness.execution_policy import (
    ExecutionPolicy,
    current_execution_policy,
    execution_policy_scope,
)
from ksadk.harness.native.adapters import StrictToolExecutor, ToolBinding
from ksadk.harness.native.ports import ToolDefinition, ToolResult
from ksadk.harness.native.state import RunPhase
from ksadk.harness.subagent_adapters.native_child_result import NativeChildResultPort
from ksadk.harness.workspace_adapters.native_isolation import (
    ChildWorkspaceError,
    DirectoryChildWorkspaces,
    builtin_state_database,
)
from ksadk.studio.native_child_publication import MERGE_SCHEMA, previous_child_publication


async def _joined(operation, *args, **kwargs):
    # File preparation/publication is owned until its blocking work terminates.
    # Cancelling an asyncio observer cannot stop an already running thread.
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    canceled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            canceled = True
    result = task.result()
    if canceled:
        raise asyncio.CancelledError
    return result


class _ChildFileTools:
    def __init__(self, tools, root):
        self.tools, self.root = tools, root

    async def execute(self, request):
        policy = current_execution_policy()
        state_root = self.root.parent / "runtime"
        isolated = (
            replace(policy, workspace_root=self.root, tool_state_root=state_root)
            if policy
            else ExecutionPolicy(workspace_root=self.root, tool_state_root=state_root)
        )
        with execution_policy_scope(isolated):
            return await self.tools.execute(request)


class StudioChildFileWorkspaces:
    """Only the host's proven SDK file tools may consume this directory grant."""

    def __init__(self, *, source, writable_root, storage, scope, store, authorize, tool_names):
        self.scope, self.store, self.authorize = scope, store, authorize
        self.tool_names = frozenset(tool_names)
        self.files = DirectoryChildWorkspaces(
            source=source,
            storage=storage,
            source_database=builtin_state_database(Path(writable_root), Path(source)),
        )

    def identity(self, scope, session_id, role):
        value = {
            "scope": scope.model_dump(mode="json"),
            "session_id": session_id,
            "role": role.model_dump(mode="json"),
            "source": str(self.files.source),
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    async def bind(self, child, tools):
        identity = self.identity(child.scope, child.child_session_id, child.role)
        try:
            self.store.load(child.scope, child.child_run_id)
            existing = True
        except KeyError:
            existing = child.continuation_of is not None
        workspace = (
            await _joined(self.files._load, self.files.storage / identity, identity)
            if existing
            else await _joined(self.files.prepare, identity)
        )
        return workspace, _ChildFileTools(tools, workspace.root), self.files.observer(workspace)

    def merge_surface(self, roles):
        reader = NativeChildResultPort(scope=self.scope, store=self.store, roles=roles)
        role_map = {role.role_id: role for role in roles}
        definition = ToolDefinition(
            name="native_child_merge",
            description=(
                "Merge an accounted completed file-writing child's isolated changes into "
                "the parent workspace. The host checks the frozen base and current files; "
                "conflicts publish nothing and require ordinary parent read/edit repair. "
                "This operation is not independent task verification."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["child_run_id"],
                "properties": {
                    "child_run_id": {"type": "string", "minLength": 1, "maxLength": 256}
                },
            },
            read_only=False,
            feedback_profile_digest="sha256:"
            + hashlib.sha256(
                (
                    "studio-child-file-merge/v2\0"
                    + json.dumps([role.model_dump(mode="json") for role in roles], sort_keys=True)
                ).encode()
            ).hexdigest(),
        )

        async def merge(request):
            parent = self.store.load(request.scope, request.run_id)
            source = reader._load_session_child(parent, request.call.arguments["child_run_id"])
            if source is None:
                return ToolResult(status="error", content={"error_code": "child_not_accounted"})
            child, _source_parent = source
            role = role_map.get(child.origin.get("role"))
            current = self.store.session_head(child.scope, child.session_id)
            if (
                role is None
                or role.workspace_access != "isolated_write"
                or child.phase != RunPhase.COMPLETED
                or child.outcome in {"failed", "blocked", "budget_exhausted"}
                or current is None
                or current.run_id != child.run_id
                or not child.workspace_revision
            ):
                return ToolResult(status="error", content={"error_code": "child_merge_unavailable"})
            identity = self.identity(child.scope, child.session_id, role)
            if child.origin.get("workspace_isolation_id") != identity:
                return ToolResult(status="error", content={"error_code": "child_workspace_unbound"})
            try:
                workspace = self.files._load(self.files.storage / identity, identity)
                if child.origin.get("workspace_base_revision") != workspace.base_revision:
                    raise ChildWorkspaceError("child_workspace_baseline_changed")
                previous = previous_child_publication(reader, parent, child)
                result = await _joined(
                    self.files.merge, workspace, expected_revision=child.workspace_revision,
                    previous=previous,
                )
            except (ChildWorkspaceError, OSError, ValueError):
                return ToolResult(
                    status="error",
                    content={
                        "error_code": "child_workspace_merge_unavailable",
                        "instruction": (
                            "Inspect the saved child workspace and delivery before retrying."
                        ),
                    },
                )
            return ToolResult(
                status="ok" if result["ok"] else "error",
                content={
                    "schema_version": MERGE_SCHEMA,
                    "child_run_id": child.run_id,
                    **result,
                },
                workspace_changed_paths=tuple(result["applied"]),
            )

        return definition, StrictToolExecutor(
            scope=self.scope, tools=(ToolBinding(definition, merge),), authorize=self.authorize
        )
