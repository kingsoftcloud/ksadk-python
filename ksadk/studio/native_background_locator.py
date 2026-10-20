"""Canonical read-only locator for background command output.

Resolves the actually-started backend and owning scope for a background command
from durable, authorized sources — never from the HTTP request, the current
environment, or a model input. This is the single trusted path shared by the
HTTP read route, the Native observer and the read/stop tools.

Cold-start capable: it does not depend on a live in-memory activation map. It
rebuilds the owning ``RunScope`` from the host's tenant/workspace (on
``studio.plugin_runs``), the run's session owner (verified against session
metadata) and the run's agent, then verifies the scope digest against the run's
pinned ``native_ref`` and loads the canonical snapshot/receipt from the durable
store.

A background command ref may originate from a different (parent) run; the
committed launcher receipt is read from ``ref.source_run_id`` (not the current
query run) and verified to share the same session/scope/operation/ref.

A receipt/ref without a provable backend is fail-closed (``unavailable``); the
backend must be one the host explicitly supports (``local_process`` / ``e2b``).
Unknown never falls back to the current environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ksadk.harness.native.state import BackgroundCommandRef, RunScope

# Backends the host explicitly supports for durable background commands. A
# sealed receipt backend not in this set is not trusted (no arbitrary
# "sandbox/*" prefix is treated as a supported backend).
_SUPPORTED_BACKENDS = frozenset({"local_process", "e2b"})

# The PluginKernel adapter (_outer_handle in kernel_adapter.py) adds this key
# to the outer native_ref to carry the provider runtime_type across the wrapper
# boundary; _provider_handle strips it and restores runtime_type before
# delegating to NativeRuntimeAdapter.snapshot, which requires runtime_type
# "harness". The HTTP locator sees the outer handle, so it must verify the
# wrapper marker value is "harness" (the Native harness provider), strip it,
# then require exact equality with the inner native_ref. Any OTHER unknown key
# is rejected (not a silent subset strip).
_PROVIDER_RUNTIME_TYPE_KEY = "_ksadk_plugin_provider_runtime_type"
_EXPECTED_PROVIDER_RUNTIME_TYPE = "harness"


def _unwrap_native_ref(native_ref: dict) -> dict | None:
    """Verify and strip the PluginKernel wrapper key from an outer native_ref.

    The wrapper marker (_ksadk_plugin_provider_runtime_type) is set by
    _outer_handle to the *inner* provider handle's runtime_type, which
    NativeRuntimeAdapter._external always sets to "harness". The outer
    handle.runtime_type is the PluginKernel's own runtime_type (which may
    differ). These two are semantically distinct: the marker proves the inner
    provider is the Native harness, the outer runtime_type is the wrapper's.
    Both are checked independently in resolve(). A missing marker, wrong type,
    a value that is not "harness", or any unknown extra field is rejected.
    """
    if not isinstance(native_ref, dict):
        return None
    if _PROVIDER_RUNTIME_TYPE_KEY not in native_ref:
        return None
    marker = native_ref[_PROVIDER_RUNTIME_TYPE_KEY]
    if not isinstance(marker, str) or marker != _EXPECTED_PROVIDER_RUNTIME_TYPE:
        return None
    inner = {k: v for k, v in native_ref.items() if k != _PROVIDER_RUNTIME_TYPE_KEY}
    return inner


def _scope_digest(scope: RunScope) -> str:
    """Stable digest matching harness.native_runtime's ``digest(self._scope)``."""
    from ksadk.harness.native.ports import digest

    return digest(scope)


@dataclass(frozen=True)
class BackgroundLocatorResult:
    """The trusted resolution of one background command query.

    ``backend`` is the actually-started backend sealed in the canonical launch
    receipt and supported by the host, or None when the record carries no
    provable backend (fail-closed). ``available`` is False when the command
    cannot be resolved to a readable state. ``scope`` is None when the run
    cannot be attributed to an authorized scope at all.
    """
    scope: RunScope | None
    task_id: str
    backend: str | None
    available: bool


class BackgroundCommandLocator:
    """Resolve a background command from durable authorized sources."""

    def __init__(self, studio: Any):
        self._studio = studio

    async def resolve(self, run_id: str, task_id: str) -> BackgroundLocatorResult | None:
        run = self._studio.event_store.get(run_id)
        if run is None or run.runtime_type != "harness":
            return None
        # Validate the runtime_handle against the RunHandle schema rather than
        # raw dict.get — a malformed handle is rejected, not partially read.
        from ksadk.runtime.adapter import RunHandle

        try:
            handle_obj = RunHandle.model_validate(run.runtime_handle or {})
        except (ValueError, TypeError):
            return BackgroundLocatorResult(scope=None, task_id=task_id, backend=None, available=False)
        native_ref = dict(handle_obj.native_ref or {})
        if native_ref.get("engine_kind") != "native":
            return None
        # Bind the RunHandle to the RunRecord: the handle's session must match
        # the run's session, and the handle's runtime_type must match the
        # RunRecord's runtime_type (the wrapper's outer type must agree with the
        # run's declared type). A tampered outer runtime_type (e.g. changed to a
        # foreign value while the marker stays "harness") is rejected here, not
        # only when the marker also changes. This mirrors the binding that
        # NativeRuntimeAdapter.snapshot:239-244 checks internally.
        if handle_obj.session_id != run.session_id:
            return BackgroundLocatorResult(scope=None, task_id=task_id, backend=None, available=False)
        if handle_obj.runtime_type != run.runtime_type:
            return BackgroundLocatorResult(scope=None, task_id=task_id, backend=None, available=False)
        # scope_digest is required (not optional): a run without it is not a
        # pinned Native run and must not be read.
        if not native_ref.get("scope_digest"):
            return BackgroundLocatorResult(
                scope=None, task_id=task_id, backend=None, available=False,
            )
        scope = await self._resolve_scope(run)
        if scope is None:
            return None
        if native_ref["scope_digest"] != _scope_digest(scope):
            return BackgroundLocatorResult(scope=scope, task_id=task_id, backend=None, available=False)
        store = self._native_store()
        if store is None:
            return None
        # The canonical Native run id is the RunHandle's top-level run_id
        # (serialized by run_service as record.runtime_handle["run_id"]). It is
        # NOT a nested native_ref field and must not fall back to the Studio run
        # id when missing — a missing pinned run id means the handle is invalid.
        native_run_id = handle_obj.run_id
        if not native_run_id:
            return BackgroundLocatorResult(scope=scope, task_id=task_id, backend=None, available=False)
        try:
            snapshot = store.load(scope, native_run_id)
        except (KeyError, ValueError, LookupError, RuntimeError):
            # A run that cannot be loaded (including StoreConflict/ScopeMismatch,
            # which are RuntimeError subclasses) is unavailable, not a 500.
            return BackgroundLocatorResult(scope=scope, task_id=task_id, backend=None, available=False)
        if snapshot.scope != scope or snapshot.session_id != handle_obj.session_id:
            return BackgroundLocatorResult(scope=scope, task_id=task_id, backend=None, available=False)
        # Full native_ref equality contract (mirrors NativeRuntimeAdapter.snapshot
        # expected): the pinned native_ref must equal the snapshot-rebuilt
        # expected dict. The outer runtime_handle.native_ref carries the wrapper
        # key _ksadk_plugin_provider_runtime_type (added by PluginKernelAdapter
        # _outer_handle); strip that one known wrapper key, then require exact
        # equality — any other unknown extra field is rejected, not silently
        # dropped.
        inner_native_ref = _unwrap_native_ref(native_ref)
        if inner_native_ref is None:
            return BackgroundLocatorResult(scope=scope, task_id=task_id, backend=None, available=False)
        expected_native_ref = {
            "configuration_digest": snapshot.configuration_digest,
            **(snapshot.origin or {}),
            "engine_kind": "native",
            "scope_digest": _scope_digest(scope),
        }
        if inner_native_ref != expected_native_ref:
            return BackgroundLocatorResult(scope=scope, task_id=task_id, backend=None, available=False)
        ref = self._find_ref(snapshot, task_id)
        if ref is None:
            return None
        backend = self._receipt_backend(store, scope, ref, current_session_id=run.session_id)
        return BackgroundLocatorResult(
            scope=scope, task_id=task_id, backend=backend, available=backend is not None,
        )

    async def _resolve_scope(self, run: Any) -> RunScope | None:
        # tenant/workspace/session_service live on studio.plugin_runs
        # (StudioPluginRuntime), not on StudioService directly.
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
        # The session owner is the trusted, persistent source (matching
        # plugin_native_activation's contract: same agent + valid user_id).
        # A missing/mismatched session does NOT fall back to a hardcoded user —
        # scope_digest match is not a substitute for session ownership.
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

    def _native_store(self) -> Any:
        composer = getattr(self._studio, "_native_product_composer", None)
        if composer is None:
            return None
        return getattr(composer, "store", None)

    @staticmethod
    def _find_ref(snapshot: Any, task_id: str) -> BackgroundCommandRef | None:
        for item in getattr(snapshot, "background_commands", ()) or ():
            ref = item.ref
            if ref.task_id == task_id:
                return ref
        return None

    @staticmethod
    def _backend_from_receipt(receipt: Any) -> str | None:
        """Extract the sealed, host-supported backend from a committed launch
        receipt's content. Single-argument pure function for callers that have
        already verified the receipt/ref. Returns None (fail-closed) when the
        receipt carries no provable backend or an unsupported backend name.
        """
        result = getattr(receipt, "result", None)
        if not isinstance(result, dict):
            return None
        content = result.get("content")
        if isinstance(content, dict):
            backend = content.get("backend")
            if isinstance(backend, str) and backend.startswith("sandbox/"):
                name = backend[len("sandbox/"):]
                if name in _SUPPORTED_BACKENDS:
                    return name
        return None

    @staticmethod
    def _receipt_backend(store: Any, scope: RunScope, ref: BackgroundCommandRef,
                         *, current_session_id: str) -> str | None:
        """Full canonical resolution: load the committed launcher receipt from
        the ref's source run, verify the source run shares the same authorized
        scope AND session, and the ref matches the sealed ref, then extract the
        backend. A foreign source run (different scope/session) or a mismatched
        ref is rejected — never return a backend for a task that does not
        belong to this run's session. Store conflicts map to None (unavailable),
        never 500."""
        try:
            source_snapshot = store.load(scope, ref.source_run_id)
            if source_snapshot.scope != scope or source_snapshot.session_id != current_session_id:
                return None
            receipt = store.get_receipt(scope, ref.source_run_id, ref.operation_key)
        except (KeyError, ValueError, LookupError, RuntimeError):
            return None
        if receipt is None or receipt.status != "committed":
            return None
        if receipt.result.get("status") != "ok":
            return None
        sealed_ref = receipt.result.get("background_command_ref")
        try:
            if BackgroundCommandRef.model_validate(sealed_ref) != ref:
                return None
        except Exception:
            return None
        return BackgroundCommandLocator._backend_from_receipt(receipt)
