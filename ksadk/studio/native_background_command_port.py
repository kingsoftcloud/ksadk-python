"""Observe background command status only through a scoped, committed launcher receipt."""

from ksadk.harness.native.background_command_contract import BackgroundCommandObservation
from ksadk.harness.native.state import BackgroundCommandRef
from ksadk.runtime_context import tool_execution_scope
from ksadk.sandbox import LocalProcessSandboxBackend, SandboxError
from ksadk.studio.native_background_locator import BackgroundCommandLocator
from ksadk.toolsets.sandbox import _background_session_key

# Reuse the canonical backend extraction shared with the HTTP read route so the
# observer and the read route never diverge on what "the started backend" means.
# The observer has already verified the receipt/ref, so it uses the pure
# single-argument extractor; the locator uses the full _receipt_backend that
# loads and verifies the source-run receipt.
_receipt_backend = BackgroundCommandLocator._backend_from_receipt


class StudioBackgroundCommandPort:
    def __init__(
        self,
        *,
        scope,
        store,
        workspace_root,
        command_state_root,
        make_background_session=None,
    ):
        self.scope = scope
        self.store = store
        self._workspace_root = workspace_root
        self._command_state_root = command_state_root
        # Factory: (backend=None) -> a background-capable session. Defaults to
        # the local_process session; the product composer supplies a
        # backend-aware factory so E2B background commands are observed too.
        self._make_background_session = make_background_session or self._local_session

    def _local_session(self, backend=None):
        return LocalProcessSandboxBackend(
            workspace_root=self._workspace_root, command_state_root=self._command_state_root,
        ).create_session(session_id=_background_session_key())

    def observe(self, request):
        if request.scope != self.scope:
            raise ValueError("background_command_scope_mismatch")
        current = self.store.load(self.scope, request.run_id)
        owned = {item.ref.task_id: item.ref for item in current.background_commands
                 if not item.notified}
        if (current.session_id != request.session_id or current.scope != request.scope
                or any(owned.get(ref.task_id) != ref for ref in request.refs)):
            raise ValueError("background_command_owner_mismatch")
        result = []
        for ref in request.refs:
            source = self.store.load(self.scope, ref.source_run_id)
            receipt = self.store.get_receipt(self.scope, ref.source_run_id, ref.operation_key)
            if (source.session_id != current.session_id or receipt is None
                    or receipt.status != "committed" or receipt.result.get("status") != "ok"
                    or BackgroundCommandRef.model_validate(
                        receipt.result.get("background_command_ref")
                    ) != ref):
                raise ValueError("background_command_launcher_receipt_mismatch")
            # Select the read entry from the actually-started backend recorded
            # in the canonical launch receipt, not from the current environment
            # or the request. This keeps the observer on the same backend that
            # launched the command even if the env changed between launch and
            # observation. A receipt without a sealed backend is fail-closed
            # (unavailable) rather than silently falling back to the env.
            launch_backend = _receipt_backend(receipt)
            if launch_backend is None:
                result.append(BackgroundCommandObservation(ref=ref, status="unavailable"))
                if len(result) == 8:
                    break
                continue
            try:
                with tool_execution_scope(session_id=current.session_id):
                    session = self._make_background_session(backend=launch_backend)
                output = session.connect_background_command(ref.task_id).read_output(max_bytes=8192)
            except (SandboxError, OSError, ValueError):
                result.append(BackgroundCommandObservation(ref=ref, status="unavailable"))
            else:
                if output.status in {"starting", "running"}:
                    continue
                result.append(BackgroundCommandObservation(
                    ref=ref, status=output.status, exit_code=output.exit_code,
                    stdout=output.stdout, stderr=output.stderr,
                    stdout_cursor=output.stdout_cursor, stderr_cursor=output.stderr_cursor,
                    output_may_be_truncated=(
                        output.stdout_cursor >= 8192 or output.stderr_cursor >= 8192
                    ),
                    # Gap evidence is optional on the backend's CommandOutput
                    # (local has no gap); use getattr so the common local path is
                    # not broken when the backend does not carry these fields.
                    evidence_incomplete=getattr(output, "evidence_incomplete", False) is True,
                    observed_exit_code=getattr(output, "observed_exit_code", None),
                ))
            if len(result) == 8:
                break
        return tuple(result)
