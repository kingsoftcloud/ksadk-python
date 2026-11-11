"""Canonical Native ownership verification for explicit session deletion.

The session-durable-state scan in ``E2BCommandStore.terminate`` proves the
frozen ``sandbox.json`` belongs to this session's owner key and that command
records carry matching launch-evidence fields. That is *not* the same as
proving the sandbox was launched by a canonical Native run: a probe can plant
matching metadata with zero rows in ``native_runs``. This module reuses the
trusted path already traversed by ``BackgroundCommandLocator`` —
RunHandle/session/scope_digest/native_ref → committed source-run receipt →
sealed ``BackgroundCommandRef`` — to verify that the sandbox_id frozen in
``sandbox.json`` was actually launched by a canonical Native run owned by this
session, before any kill RPC.

This is a Native host/product boundary check. It does not move canonical
ownership into the generic Sandbox SDK; the standalone ``E2BCommandStore``
lifecycle (launch/query/stop/cold/delete) is preserved. The Composer accepts
this verifier as a host-supplied port (the Composer alone has no trusted
session/run input); the Studio service constructs it from
``event_store.list_runs`` + the trusted session service + the Composer's
durable ``RunStore``, exactly the sources ``BackgroundCommandLocator`` uses.
"""

from __future__ import annotations

from typing import Any, Protocol

from ksadk.harness.native.state import BackgroundCommandRef, RunScope


class NativeBackgroundOwnershipVerifier(Protocol):
    """Verify a frozen sandbox_id was launched by a canonical Native run owned
    by the given session. Returns True only when a committed launch receipt for
    this session seals a ``BackgroundCommandRef`` whose ``task_id`` corresponds
    to a command record whose ``sandbox_id`` equals the target. Returns False
    (fail closed, zero backend calls) when there is no canonical Native run, no
    committed receipt, or the linkage cannot be proven.

    Async because the trusted session-service lookup is async (the same lookup
    ``BackgroundCommandLocator._resolve_scope`` awaits). The Composer's
    ``terminate_owner`` is called from the async ``delete_session`` path.
    """

    async def verify_sandbox_launched_by_session(
        self, *, session_id: str, sandbox_id: str,
    ) -> bool: ...


class StudioNativeBackgroundOwnershipVerifier:
    """Concrete verifier backed by the Studio event store + durable RunStore.

    Enumerates the session's harness runs, rebuilds the authorized scope (same
    trusted sources as ``BackgroundCommandLocator._resolve_scope``), loads each
    canonical Native snapshot's ``background_commands``, and for each ref
    verifies the committed launch receipt seals a matching ref whose
    ``task_id`` links (via a command record in the session's owner directory) to
    the target ``sandbox_id``.

    The verifier holds a reference to the Studio service (for event_store +
    plugin_runs + session_service) and the Composer's durable RunStore. It does
    not depend on a live activation map; it is cold-start capable.
    """

    def __init__(
        self, studio: Any, *, native_store: Any, workspace_root: Any,
        background_declaration: Any = None,
    ):
        self._studio = studio
        self._native_store = native_store
        # workspace_root is the Composer's workspace root; the verifier builds a
        # read-only E2BCommandStore for the session to look up task_id → sandbox_id
        # linkage. The declaration is threaded so the store's owner key resolves
        # identically to the one terminate_owner builds.
        self._workspace_root = workspace_root
        self._declaration = background_declaration

    def _command_store_for_session(self, session_id: str) -> Any:
        """Build a read-only E2BCommandStore matching terminate_owner's owner
        key, for the task_id → sandbox_id linkage lookup."""
        from ksadk.runtime_context import tool_execution_scope
        from ksadk.sandbox.e2b_background import E2BCommandStore
        from ksadk.studio.native_product import identity_workspace_root
        from ksadk.toolsets.sandbox import _background_session_key

        state_root = self._workspace_root / ".harness-tools" / "background"
        with tool_execution_scope(session_id=session_id):
            return E2BCommandStore(
                state_root=state_root,
                workspace_root=identity_workspace_root(
                    self._workspace_root / ".harness-tools" / "workspace"
                ),
                session_id=_background_session_key(),
                source_config={"source_kind": "e2b"},
                declaration=self._declaration,
            )

    async def verify_sandbox_launched_by_session(
        self, *, session_id: str, sandbox_id: str,
    ) -> bool:
        if not isinstance(sandbox_id, str) or not sandbox_id:
            return False
        event_store = getattr(self._studio, "event_store", None)
        if event_store is None:
            return False
        command_store = self._command_store_for_session(session_id)
        runs = event_store.list_runs(session_id=session_id)
        for run in runs:
            if getattr(run, "runtime_type", None) != "harness":
                continue
            if await self._run_proves_launch(
                run, session_id=session_id, sandbox_id=sandbox_id,
                command_store=command_store,
            ):
                return True
        return False

    async def _run_proves_launch(
        self, run: Any, *, session_id: str, sandbox_id: str, command_store: Any,
    ) -> bool:
        from ksadk.runtime.adapter import RunHandle
        from ksadk.studio.native_background_locator import (
            _scope_digest,
            _unwrap_native_ref,
        )

        try:
            handle_obj = RunHandle.model_validate(run.runtime_handle or {})
        except (ValueError, TypeError):
            return False
        native_ref = dict(handle_obj.native_ref or {})
        if native_ref.get("engine_kind") != "native":
            return False
        if handle_obj.session_id != run.session_id:
            return False
        if handle_obj.runtime_type != run.runtime_type:
            return False
        if not native_ref.get("scope_digest"):
            return False
        # Rebuild the authorized scope from the same trusted sources as the
        # locator's _resolve_scope (async session-service lookup).
        scope = await self._resolve_scope(run)
        if scope is None:
            return False
        if native_ref["scope_digest"] != _scope_digest(scope):
            return False
        native_run_id = handle_obj.run_id
        if not native_run_id:
            return False
        try:
            snapshot = self._native_store.load(scope, native_run_id)
        except (KeyError, ValueError, LookupError, RuntimeError):
            return False
        if snapshot.scope != scope or snapshot.session_id != handle_obj.session_id:
            return False
        # Full native_ref equality (strip the provider wrapper key), mirroring
        # the locator.
        inner_native_ref = _unwrap_native_ref(native_ref)
        if inner_native_ref is None:
            return False
        expected_native_ref = {
            "configuration_digest": snapshot.configuration_digest,
            **(snapshot.origin or {}),
            "engine_kind": "native",
            "scope_digest": _scope_digest(scope),
        }
        if inner_native_ref != expected_native_ref:
            return False
        # For each background command ref in this canonical run, verify the
        # committed launch receipt seals a matching ref AND that the ref's
        # task_id links to the target sandbox_id via a command record this
        # session owns.
        for item in getattr(snapshot, "background_commands", ()) or ():
            if self._ref_proves_sandbox(
                item.ref, scope=scope, current_session_id=session_id,
                sandbox_id=sandbox_id, command_store=command_store,
            ):
                return True
        return False

    def _ref_proves_sandbox(
        self, ref: BackgroundCommandRef, *, scope: RunScope,
        current_session_id: str, sandbox_id: str, command_store: Any,
    ) -> bool:
        # Reuse the locator's COMPLETE canonical receipt contract exactly
        # (locator.py:245-272 _receipt_backend): source run shares the same
        # authorized scope AND session, receipt is committed + ok, sealed ref
        # matches, and the sealed backend is a host-supported e2b. Do not mirror
        # the comments — call the same trusted helpers.
        from ksadk.studio.native_background_locator import (
            BackgroundCommandLocator,
        )

        try:
            source_snapshot = self._native_store.load(scope, ref.source_run_id)
            if source_snapshot.scope != scope or (
                source_snapshot.session_id != current_session_id
            ):
                return False
            receipt = self._native_store.get_receipt(
                scope, ref.source_run_id, ref.operation_key,
            )
        except (KeyError, ValueError, LookupError, RuntimeError):
            return False
        if receipt is None or receipt.status != "committed":
            return False
        if receipt.result.get("status") != "ok":
            return False
        sealed_ref = receipt.result.get("background_command_ref")
        try:
            if BackgroundCommandRef.model_validate(sealed_ref) != ref:
                return False
        except Exception:
            return False
        # The sealed backend must be e2b (the only background sandbox backend
        # this verifier governs). Reuse the locator's pure extractor.
        backend = BackgroundCommandLocator._backend_from_receipt(receipt)
        if backend != "e2b":
            return False
        # Canonical launch → sandbox linkage: the ref's task_id identifies the
        # command record in the session's owner directory; that record's
        # sandbox_id must equal the frozen target. The receipt content also
        # carries the task_id; it must agree with the sealed ref's task_id.
        content = receipt.result.get("content")
        if not isinstance(content, dict):
            return False
        receipt_task_id = content.get("task_id")
        if not isinstance(receipt_task_id, str) or receipt_task_id != ref.task_id:
            return False
        return command_store.record_sandbox_id_for_task(ref.task_id) == sandbox_id

    async def _resolve_scope(self, run: Any) -> RunScope | None:
        # Mirror BackgroundCommandLocator._resolve_scope exactly: the trusted
        # session-service lookup is the owner source; no hardcoded fallback.
        plugin_runs = getattr(self._studio, "plugin_runs", None)
        if plugin_runs is None:
            return None
        tenant_id = getattr(plugin_runs, "_native_tenant_id", "") or ""
        workspace_id = getattr(plugin_runs, "_native_workspace_id", "") or ""
        if not tenant_id or not workspace_id:
            return None
        session_service = getattr(plugin_runs, "_session_service", None)
        if session_service is None:
            return None
        try:
            session = await session_service.get_session_metadata(run.session_id)
        except Exception:
            return None
        if session is None or session.agent_id != run.agent_id or not session.user_id:
            return None
        try:
            return RunScope(
                tenant_id=tenant_id, workspace_id=workspace_id,
                user_id=session.user_id, agent_id=run.agent_id,
            )
        except (KeyError, ValueError, TypeError):
            return None
