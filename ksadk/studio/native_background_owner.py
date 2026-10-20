"""Bind background sandbox lifecycle to verified Studio session ownership."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ksadk.studio.native_background_connection import BackgroundHostDeclaration
from ksadk.toolsets.workspace_identity import identity_workspace_root


def build_terminate_kill(
    declaration: BackgroundHostDeclaration | None,
) -> Callable[[str], bool] | None:
    """Build a bounded kill callback for owner termination from a host declaration.

    The declaration's typed target + resolved key construct the backend that
    reconnects and kills the sandbox. Authority is NOT checked here: the
    ``E2BCommandStore.terminate`` method compares the frozen sandbox.json
    typed target against the declaration in full before this callback is ever
    invoked, so the kill only runs against an authority the host has proven it
    owns. Returns None when no declaration is available (terminate then reports
    ``unknown`` without a backend call).
    """
    if declaration is None:
        return None
    try:
        from ksadk.sandbox.backends.e2b import E2BSandboxBackend
        from ksadk.sandbox.factory import sandbox_spec_from_env
        from ksadk.studio.native_background_connection import make_explicit_connection

        connection = make_explicit_connection(declaration)
        backend = E2BSandboxBackend(spec=sandbox_spec_from_env(), connection=connection)
    except Exception:
        return None

    def kill(sandbox_id: str) -> bool:
        session = backend.reconnect_session(session_locator=sandbox_id)
        return bool(session.kill())

    return kill


class StudioBackgroundOwner:
    """Bind background sandbox lifecycle to verified Studio session ownership."""

    async def terminate_owner(self, session_id: str) -> str:
        """Explicitly terminate the persistent background sandbox for one owner.

        Returns: "terminated", "confirmed_absent", "not_e2b", "no_owner",
        "unknown", "failed", "timeout".
        Not called on ordinary aclose; only on explicit owner termination.

        ``session_id`` is the canonical Studio session id, sourced by the host
        (StudioService.delete_session) from the trusted session service. It binds
        to the durable owner key ``sha256([workspace, "e2b", "ksadk-session:{id}])``
        via ``tool_execution_scope`` + ``_background_session_key``, so terminate
        targets exactly the owner that launched. The store's ownership checks
        (rec.owner / sandbox_data.owner / sandbox_id↔command linkage) prove the
        frozen sandbox belongs to this session's owner; the typed declaration
        proves account authority.

        **Canonical Native ownership** — in addition to the session-durable-state
        checks, the frozen ``sandbox_id`` must be proven to have been launched by
        a canonical Native run owned by this session. This reuses the trusted path
        ``BackgroundCommandLocator`` traverses (RunHandle/scope/native_ref →
        committed source-run receipt → sealed BackgroundCommandRef → task_id →
        record sandbox_id). A sandbox with matching session metadata but zero
        canonical Native runs (planted metadata) is ``unknown`` with zero backend
        calls. The verifier is a host-supplied port; without it, any existing
        sandbox is ``unknown`` (the Composer alone has no trusted session/run
        input). ``no_owner``/``not_e2b`` (no target sandbox to prove) bypass the
        canonical check and are returned by the store directly. A
        damaged/foreign/missing-evidence state is ``unknown`` (metadata retained),
        never ``confirmed_absent``; only a provably valid canonical owner is
        cleaned up. typed account/session JSON is NOT canonical RunHandle/receipt.
        """
        from ksadk.runtime_context import tool_execution_scope
        from ksadk.sandbox.e2b_background import E2BCommandStore, _json
        from ksadk.toolsets.sandbox import _background_session_key

        state_root = self._workspace_root / ".harness-tools" / "background"
        with tool_execution_scope(session_id=session_id):
            store = E2BCommandStore(
                state_root=state_root,
                workspace_root=identity_workspace_root(
                    self._workspace_root / ".harness-tools" / "workspace"
                ),
                session_id=_background_session_key(),
                source_config={"source_kind": "e2b"},
                kill_sandbox=self._owner_termination_kill(),
                declaration=self._background_declaration,
            )
        # Canonical Native ownership: before the store's kill RPC, prove the
        # frozen sandbox_id was launched by a canonical Native run owned by this
        # session. Only an existing e2b sandbox with a provable sandbox_id
        # requires this check; no_owner/not_e2b have no target to prove.
        sandbox_file = store._sandbox_root / "sandbox.json"
        if sandbox_file.is_file() and not sandbox_file.is_symlink():
            try:
                sandbox_data = _json(sandbox_file)
            except Exception:
                return "unknown"
            sandbox_id = sandbox_data.get("sandbox_id", "")
            if sandbox_id and sandbox_data.get("owner") == store.owner:
                if self._ownership_verifier is None:
                    return "unknown"
                proven = await self._ownership_verifier.verify_sandbox_launched_by_session(
                    session_id=session_id,
                    sandbox_id=sandbox_id,
                )
                if not proven:
                    return "unknown"
        return store.terminate()

    def _make_background_session(self, workspace_root: Path, command_state_root: Path):
        """Factory for the Native background observer, backend-aware.

        Returns a callable that builds a durable background session for the
        given backend (taken from the canonical launch receipt) or the current
        backend. It carries this Composer's host-declared typed connection so
        the observer reads/stops under the same authority as launch. It must
        be invoked inside the owning session's ``tool_execution_scope`` so the
        session id resolves correctly.
        """
        from ksadk.toolsets.sandbox import make_background_session

        root = identity_workspace_root(workspace_root)
        state_root = command_state_root
        declaration = self._background_declaration
        return lambda backend=None: make_background_session(
            workspace_root=root,
            state_root=state_root,
            backend=backend,
            declaration=declaration,
        )
