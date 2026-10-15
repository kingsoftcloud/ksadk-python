"""Bind a read-only Native workspace observer to one Studio activation scope."""

from __future__ import annotations

from pathlib import Path

from ksadk.harness.native.ports import WorkspaceObservation, WorkspaceRequest
from ksadk.harness.native.state import RunScope
from ksadk.harness.stores.native_contract import RunStore
from ksadk.harness.workspace_adapters.native_directory import LocalWorkspacePort


class StudioNativeWorkspacePort:
    """Capture the locked workspace only for a Run owned by this activation."""

    def __init__(self, *, root: Path, scope: RunScope, store: RunStore) -> None:
        self._root = root.resolve()
        self._scope = scope.model_copy(deep=True)
        self._store = store
        # Browser screenshots are host observation artifacts. Their changing
        # bytes must not look like the Agent changed the delivered project.
        self._delegate = LocalWorkspacePort(self._root, ignored_path_parts=(".artifacts",))

    async def capture(self, request: WorkspaceRequest) -> WorkspaceObservation:
        if request.scope != self._scope:
            raise ValueError("native_workspace_scope_mismatch")
        try:
            run = self._store.load(self._scope, request.run_id)
        except Exception:
            return WorkspaceObservation(
                operation_key=request.operation_key,
                status="unavailable",
                reason="run_unavailable",
            )
        if run.scope != self._scope or run.run_id != request.run_id:
            raise ValueError("native_workspace_run_mismatch")
        if self._root.resolve() != self._root:
            return WorkspaceObservation(
                operation_key=request.operation_key,
                status="unavailable",
                reason="workspace_root_changed",
            )
        return await self._delegate.capture(request)
