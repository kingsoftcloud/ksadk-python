"""Host-owned durable command records and sessions for the E2B backend.

A detached supervisor captures each remote command stream in private host
files, keyed by session owner. Handles expose UTF-8 output cursors, idempotent
startup, cooperative cancellation and conservative unknown outcomes.

Persistent sandboxes are never registered with the disposable SandboxRegistry
or killed on atexit: cleanup is an explicit host action (E2BCommandStore.close).
A host that dies without cleanup leaves sandbox cleanup to the remote TTL.
See _e2b_background_supervisor for cross-process durability and reconnect limits.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ksadk.sandbox._e2b_background_config import (
    _e2b_kill_sandbox as _e2b_kill_sandbox,
)
from ksadk.sandbox._e2b_background_config import (
    e2b_source_config as e2b_source_config,
)
from ksadk.sandbox._e2b_background_files import (
    _append_output as _append_output,
)
from ksadk.sandbox._e2b_background_files import (
    _atomic_json,
    _json,
    _open_private,
    _private_directory,
    _read_window,
    _read_window_all,
)
from ksadk.sandbox._e2b_background_files import (
    _finalize_output_from_log as _finalize_output_from_log,
)
from ksadk.sandbox._e2b_background_streams import (
    BackgroundStream as BackgroundStream,
)
from ksadk.sandbox._e2b_background_streams import (
    _disconnect_stream as _disconnect_stream,
)
from ksadk.sandbox._e2b_background_streams import (
    _E2BStreamSource,
    _kill_rpc_timeout,
    _LocalSubprocessStreamSource,
    _safe_kill,
)
from ksadk.sandbox._e2b_background_streams import (
    _PipeStream as _PipeStream,
)
from ksadk.sandbox._e2b_background_streams import (
    _Result as _Result,
)
from ksadk.sandbox._e2b_background_streams import (
    _StreamAdapter as _StreamAdapter,
)
from ksadk.sandbox._e2b_background_supervisor import _settle_unknown_safe, _supervise
from ksadk.sandbox.base import SandboxCommandResult, SandboxError

if TYPE_CHECKING:
    from ksadk.studio.native_background_connection import BackgroundHostDeclaration

logger = logging.getLogger(__name__)
e2b_source_config.__module__ = __name__

_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "output_limit", "unknown"}
_COMMAND_ID = __import__("re").compile(r"[a-f0-9]{64}\Z")
# A recovery reservation is durable but not permanent: if the claiming host
# crashes before its supervisor takes over (or the spawn fails), a new host may
# reclaim after this TTL so an orphaned claim cannot block recovery forever.
_RECOVERY_CLAIM_TTL_SECONDS = 30.0


# Command handles read output and probe the cross-process supervisor lock.


@dataclass(frozen=True)
class CommandOutput:
    status: str
    stdout: str
    stderr: str
    stdout_cursor: int
    stderr_cursor: int
    exit_code: int | None
    cancel_requested: bool = False
    # Exposed so the observer/HTTP can report that a reconnect gap left the
    # outcome with incomplete evidence, and what exit code was observed but
    # could not be trusted as complete.
    evidence_incomplete: bool = False
    observed_exit_code: int | None = None
    outcome_reason: str = ""


class E2BBackgroundCommand:
    """A host-owned handle to one remote E2B background command.

    The locator (``command_id``) is the only identity exposed to the model and
    the native layer; the remote pid stays internal. Output is read from host
    files via UTF-8 cursors. Liveness is cross-process: the detached supervisor
    holds ``supervisor.lock`` with an exclusive flock while it runs.
    """

    def __init__(self, store: "E2BCommandStore", directory: Path, *, command_id: str):
        self._store = store
        self._directory = directory
        self.command_id = command_id
        self._record()  # Refuse a handle from another session/root.

    def _record(self) -> dict[str, Any]:
        record = _json(self._directory / "record.json")
        if record.get("owner") != self._store.owner or record.get("command_id") != self.command_id:
            raise SandboxError("Background command does not belong to this session")
        return record

    @property
    def timeout_seconds(self) -> int:
        return self._record()["process_timeout_seconds"]

    @property
    def process_id(self) -> int:
        # The supervisor's own pid, used only as a liveness/compatibility
        # locator. No action sends signals to it; cancellation writes a marker
        # that the supervisor observes and translates into a remote kill.
        process_id = self._record().get("supervisor_pid")
        if type(process_id) is not int or process_id < 1:
            raise SandboxError("Background command supervisor identity is unavailable")
        return process_id

    def read_output(
        self, *, stdout_cursor: int = 0, stderr_cursor: int = 0, max_bytes: int = 65536
    ) -> CommandOutput:
        if type(max_bytes) is not int or not 4 <= max_bytes <= 65536:
            raise SandboxError("Command output window must be between 4 and 65536 bytes")
        # Cross-process liveness: if the advisory lock is free, no live
        # supervisor owns the command. A non-terminal status without a live
        # supervisor cannot be confirmed and must not be retried.
        with os.fdopen(
            _open_private(self._directory / "supervisor.lock", os.O_RDWR | os.O_CREAT), "r+"
        ) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                supervisor_alive = True
            else:
                supervisor_alive = False
            record = self._record()
            status = str(record.get("status", "unknown"))
            if status not in _TERMINAL and not supervisor_alive:
                if status != "starting" or time.time() - record["created_at"] > 5:
                    status = "unknown"
        stdout, stdout_end = _read_window(self._directory / "stdout", stdout_cursor, max_bytes)
        stderr, stderr_end = _read_window(self._directory / "stderr", stderr_cursor, max_bytes)
        return CommandOutput(
            status=status,
            stdout=stdout,
            stderr=stderr,
            stdout_cursor=stdout_end,
            stderr_cursor=stderr_end,
            exit_code=record.get("exit_code") if status in _TERMINAL else None,
            cancel_requested=(self._directory / "cancel").exists(),
            evidence_incomplete=bool(record.get("evidence_incomplete")),
            observed_exit_code=record.get("observed_exit_code"),
            outcome_reason=str(record.get("outcome_reason") or record.get("observed_reason") or ""),
        )

    def request_cancel(self) -> CommandOutput:
        output = self.read_output()
        if output.status == "unknown":
            record = self._record()
            if (record.get("pid") and record.get("sandbox_id")
                    and self._store._authority_proven()):
                source = _make_stream_source(self._store._source_config,
                                             record["sandbox_id"], self._store._sandbox_root)
                confirmed = _safe_kill(source, int(record["pid"]), "cancel")
                record["remote_cancel_confirmed"] = confirmed
                _atomic_json(self._directory / "record.json", record)
                with os.fdopen(_open_private(
                    self._directory / "cancel", os.O_WRONLY | os.O_CREAT
                ), "w"):
                    pass
            return self.read_output()
        if output.status not in _TERMINAL:
            with os.fdopen(
                _open_private(self._directory / "cancel", os.O_WRONLY | os.O_CREAT), "w"
            ):
                pass
        return self.read_output()

    def wait(self, timeout: float | None = None) -> SandboxCommandResult:
        started = time.monotonic()
        while (output := self.read_output()).status not in _TERMINAL:
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError("Background command is still running")
            time.sleep(0.05)
        if output.status == "unknown":
            raise SandboxError("Background command outcome is unknown; do not repeat the action")
        stdout, _ = _read_window_all(self._directory / "stdout")
        stderr, _ = _read_window_all(self._directory / "stderr")
        return SandboxCommandResult(stdout=stdout, stderr=stderr, exit_code=output.exit_code)

    def kill(self) -> bool:
        self.request_cancel()
        try:
            self.wait(timeout=3)
        except (TimeoutError, SandboxError):
            return False
        return self.read_output().status == "cancelled"


# Command store: launch, recover and connect through detached supervisors.


class E2BCommandStore:
    """Host-owned command records for one session, backed by detached supervisors."""

    def __init__(
        self,
        *,
        state_root: Path,
        workspace_root: Path,
        session_id: str,
        source_config: dict[str, Any],
        kill_sandbox: Callable[[str], None] | None = None,
        declaration: BackgroundHostDeclaration | None = None,
    ) -> None:
        if os.name != "posix":
            raise SandboxError("E2B background commands require POSIX process controls")
        root = state_root.expanduser().absolute()
        workspace = workspace_root.resolve()
        if root.resolve() == workspace or workspace in root.resolve().parents:
            raise SandboxError("Background command state must be outside the editable workspace")
        self.owner = hashlib.sha256(
            json.dumps([str(workspace), "e2b", session_id], separators=(",", ":")).encode()
        ).hexdigest()
        self.directory = root / "commands" / self.owner
        _private_directory(self.directory)
        self._sandbox_root = root / "sandboxes" / self.owner
        _private_directory(self._sandbox_root)
        self._source_config = source_config
        self._kill_sandbox = kill_sandbox
        # The host-declared typed connection target + credential resolver.
        # Authority for read/stop/recover/terminate is the full typed identity
        # (connection_ref/tenant_ref/principal_ref/endpoint/auth_mode) frozen in
        # sandbox.json at launch, matched against this declaration. None means
        # the E2B background capability is not declared; any frozen typed target
        # then has no provable authority (unknown, zero backend calls).
        self._declaration = declaration

    def _has_sandbox_state(self) -> bool:
        """True iff a sandbox.json file exists for this owner (regardless of
        whether it carries a typed connection). Distinguishes a genuine first
        launch (no sandbox.json at all) from an existing sandbox state that
        merely lacks a provable typed identity (legacy/damaged)."""
        sandbox_file = self._sandbox_root / "sandbox.json"
        return sandbox_file.is_file() and not sandbox_file.is_symlink()

    def _frozen_target(self) -> Any:
        """Return the typed BackgroundConnectionTarget frozen in sandbox.json.

        None when sandbox.json is absent, corrupt, or carries no typed identity
        (a legacy api_url/domain/traffic_source record). Callers treat None as
        ``unknown`` — never as an absent authority. Use :meth:`_has_sandbox_state`
        to tell a genuine first launch (no file) from a legacy/damaged existing
        state (file present, no typed identity).
        """
        if not self._has_sandbox_state():
            return None
        try:
            data = _json(self._sandbox_root / "sandbox.json")
        except Exception:
            return None
        from ksadk.studio.native_background_connection import BackgroundConnectionTarget

        return BackgroundConnectionTarget.from_dict(data.get("connection"))

    def _authority_proven(self) -> bool:
        """True iff the host declaration proves authority over the existing
        sandbox frozen in sandbox.json — the full typed identity matches the
        declaration. This is the gate for operations that TOUCH an existing
        sandbox (recover reconnect, close kill, terminate kill).

        False (caller refuses with 0 side effects) when:
        - a typed target is frozen but the declaration is missing/mismatched;
        - no typed target is frozen and the source is production E2B (an
          existing sandbox we cannot prove authority over — legacy/damaged).

        The ``local_subprocess`` test seam has no typed connection by design
        (it is the explicit test harness, not a real E2B sandbox with an
        account), so a missing typed target there is always allowed. That
        bypass is scoped to the test seam only and must not let production E2B
        through an unprovable state.
        """
        frozen = self._frozen_target()
        if frozen is not None:
            if self._declaration is None:
                return False
            return self._declaration.target.authority_matches(frozen)
        # No typed target: production E2B cannot prove authority over an existing
        # sandbox; the local_subprocess test seam (source_kind or stream) has no
        # real account to protect.
        cfg = self._source_config
        return (cfg.get("source_kind") == "local_subprocess"
                or cfg.get("stream") == "local_subprocess")

    def _assert_authority(self) -> None:
        """Fail closed before touching an existing sandbox unless the host is
        the authority, and allow a genuine first launch.

        - A typed target frozen in sandbox.json requires the host declaration to
          match it in full (same connection_ref/tenant_ref/principal_ref/
          endpoint/auth_mode + E2B transport). A different tenant/principal at
          the same endpoint, or no declaration, is refused.
        - No typed target on production E2B: a genuine first launch (no
          sandbox.json at all) has no existing sandbox to protect and is
          allowed; an existing sandbox.json without a typed identity
          (legacy/damaged) is an unprovable existing state → refused (unknown,
          zero backend calls).
        - The ``local_subprocess`` test seam has no typed connection by design
          and is allowed through; that bypass is scoped to the test seam and
          must not let production E2B through an unprovable state.
        """
        frozen = self._frozen_target()
        if frozen is not None:
            if self._declaration is None:
                raise SandboxError("E2B background requires a host-declared connection target")
            if not self._declaration.target.authority_matches(frozen):
                raise SandboxError("E2B background connection authority mismatch")
            return
        # No typed target. The test seam (local_subprocess stream) has no real
        # account; a missing typed target is its normal state, not a damaged sandbox.
        cfg = self._source_config
        if cfg.get("source_kind") == "local_subprocess" or cfg.get("stream") == "local_subprocess":
            return
        # Production E2B: a genuine first launch (no sandbox.json) is allowed;
        # an existing sandbox.json with no typed identity is unprovable → refuse.
        if self._has_sandbox_state():
            raise SandboxError("E2B background sandbox has no provable typed connection")
        return  # first launch

    def record_sandbox_id_for_task(self, task_id: str) -> str | None:
        """Return the sandbox_id a command record launched under, by task_id.

        Pure read used by the canonical Native ownership verifier to link a
        sealed ``BackgroundCommandRef.task_id`` to the ``sandbox_id`` frozen in
        ``sandbox.json``. Returns None when the task is not owned by this
        session, has no record, or the record carries no sandbox_id. Never
        raises — a missing/inconsistent link is None (the verifier treats None
        as "not proven", fail closed).
        """
        if not isinstance(task_id, str) or not _COMMAND_ID.fullmatch(task_id):
            return None
        directory = self.directory / task_id
        if directory.is_symlink() or not (directory / "record.json").is_file():
            return None
        try:
            rec = _json(directory / "record.json")
        except Exception:
            return None
        if rec.get("owner") != self.owner or rec.get("command_id") != task_id:
            return None
        sandbox_id = rec.get("sandbox_id")
        return sandbox_id if isinstance(sandbox_id, str) and sandbox_id else None

    def connect(self, command_id: str) -> E2BBackgroundCommand:
        if not isinstance(command_id, str) or not _COMMAND_ID.fullmatch(command_id):
            raise SandboxError("Invalid background command identity")
        directory = self.directory / command_id
        if directory.is_symlink() or not (directory / "record.json").is_file():
            raise SandboxError("Background command is not owned by this session")
        return E2BBackgroundCommand(self, directory, command_id=command_id)

    def connect_or_recover(self, command_id: str) -> E2BBackgroundCommand:
        """Connect to a command, reattaching a supervisor if the prior one died.

        This is the product-path entry: ordinary reads and the recovery reattach
        share one owned path. An unknown outcome is never re-executed; recovery
        only reconnects to the existing remote command. The host declaration's
        typed identity must match the frozen launch target before any backend
        reconnect is attempted — a different authority cannot read another
        authority's command.
        """
        self._assert_authority()
        handle = self.connect(command_id)
        record = handle._record()
        status = str(record.get("status", "unknown"))
        # A non-terminal status with no live supervisor and a known remote
        # identity is recoverable; otherwise return the handle as-is.
        if status in _TERMINAL:
            return handle
        if not record.get("pid") or not record.get("sandbox_id"):
            return handle
        if _supervisor_lock_held(handle._directory):
            return handle  # supervisor alive
        return self.recover(command_id)

    def start(
        self,
        *,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: int,
        operation_key: str | None = None,
    ) -> E2BBackgroundCommand:
        command_id = hashlib.sha256(
            (self.owner + ":" + (operation_key or uuid4().hex)).encode()
        ).hexdigest()
        digest = hashlib.sha256(
            json.dumps([command, cwd, env, timeout], sort_keys=True).encode()
        ).hexdigest()
        with os.fdopen(
            _open_private(self.directory / "launch.lock", os.O_RDWR | os.O_CREAT), "r+"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # A re-launch by a different authority (different typed identity) must
            # not reuse or re-drive another authority's command. The first launch
            # has no frozen target and is allowed through.
            self._assert_authority()
            directory = self.directory / command_id
            if directory.exists():
                handle = self.connect(command_id)
                if handle._record().get("request_digest") != digest:
                    raise SandboxError(
                        "Background command operation identity has different arguments"
                    )
                return handle  # Even an unknown launch is never executed a second time.
            _private_directory(directory)
            for name in ("stdout", "stderr", "supervisor.lock"):
                os.close(_open_private(directory / name, os.O_RDWR | os.O_CREAT | os.O_EXCL))
            _atomic_json(
                directory / "record.json",
                {
                    "version": 1,
                    "owner": self.owner,
                    "command_id": command_id,
                    "request_digest": digest,
                    "process_timeout_seconds": timeout,
                    "status": "starting",
                    "created_at": time.time(),
                    "backend": self._source_config.get("source_kind") or "e2b",
                },
            )
            handle = self.connect(command_id)
            self._spawn_supervisor(handle, mode="start", generation=0,
                                   request={"command": command, "cwd": cwd, "env": env})
        deadline = time.monotonic() + 5
        while handle._record()["status"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.02)
        return handle

    def recover(self, command_id: str) -> E2BBackgroundCommand:
        """Reattach after the previous supervisor died mid-command.

        Reconnects to the remote command via ``sandbox_id`` + ``pid``. Output
        produced during the disconnect gap is not replayed by E2B; the
        supervisor captures only post-reconnect output. The outcome stays
        ``unknown`` with ``evidence_incomplete`` even if a terminal exit code is
        observed, because the gap output is missing. Never relaunches the
        command.

        ``recover.lock`` serializes the spawn decision across concurrent hosts.
        The parent holds it while spawning the child and waits for the child to
        take ``supervisor.lock`` before releasing ``recover.lock``, so a second
        host arriving in the gap sees the lock held and does not spawn again.
        A generation counter fences stale supervisors.
        """
        handle = self.connect(command_id)
        record = handle._record()
        if str(record.get("status", "unknown")) in _TERMINAL:
            return handle
        if not record.get("pid") or not record.get("sandbox_id"):
            return handle  # Cannot reconnect without a remote identity.
        # Reconnecting touches an existing sandbox. Require proven authority
        # (full typed identity of the frozen sandbox.json matched to the host
        # declaration). An unprovable authority — a missing/legacy typed target
        # on production E2B, or a mismatched/missing declaration — settles to a
        # truthful ``unknown`` with zero backend side effects (no spawn, no
        # kill) rather than reconnecting to a sandbox this host cannot prove it
        # owns. This governs recover() as a public entry uniformly with
        # connect_or_recover/close/terminate.
        if not self._authority_proven():
            self._settle_unknown(handle, "recovery authority unprovable")
            return handle
        with os.fdopen(
            _open_private(handle._directory / "recover.lock", os.O_RDWR | os.O_CREAT), "r+"
        ) as rlock:
            fcntl.flock(rlock, fcntl.LOCK_EX)
            # Re-check inside the recover lock: a concurrent host may have
            # already persisted a reservation or a live supervisor may exist.
            record = handle._record()
            if str(record.get("status", "unknown")) in _TERMINAL:
                return handle
            if _supervisor_lock_held(handle._directory):
                return handle  # A live supervisor owns it; nothing to recover.
            claimed_gen = record.get("recovery_claim_generation")
            if claimed_gen is not None:
                # A reservation exists. If it is stale (the claiming host crashed
                # before its supervisor took over, or the spawn failed), a new
                # host may reclaim it after the reservation TTL. This prevents a
                # durable-but-orphaned claim from blocking recovery forever.
                claim_age = time.time() - float(record.get("recovery_claim_at") or 0)
                if claim_age < _RECOVERY_CLAIM_TTL_SECONDS:
                    # The durable claim already prevents a second spawn. If a
                    # supervisor is already live, return immediately; if not, the
                    # claim's supervisor will take over (or expire after TTL for
                    # reclaim). Do not block other hosts in a wait window — the
                    # claim is the serialization mechanism, not the wait.
                    return handle
                # Stale claim: clear it and fall through to spawn a fresh
                # reservation. The new generation must advance past the stale
                # claim's generation (not just record.generation, which the
                # stale claim may not have confirmed), so a delayed child from
                # the stale claim cannot collide with the new reservation's
                # token.
                stale_claim_gen = int(claimed_gen or 0)
                record.pop("recovery_claim_generation", None)
                record.pop("recovery_claim_at", None)
                record["_stale_claim_floor"] = stale_claim_gen
            # Persist a durable reservation before spawning. This survives the
            # recover.lock being released (e.g. after the wait window), so a
            # second host arriving later sees the claim and does not spawn again.
            # Generation is monotonically increasing across all reservations:
            # max of the confirmed generation, any prior claim, and any stale
            # claim floor, plus one. This guarantees a delayed child from an
            # older reservation always has a strictly smaller token than any
            # later reservation.
            prior_gen = int(record.get("generation") or 0)
            prior_claim = int(record.get("recovery_claim_generation") or 0)
            stale_floor = int(record.get("_stale_claim_floor") or 0)
            generation = max(prior_gen, prior_claim, stale_floor) + 1
            record["recovery_claim_generation"] = generation
            record["recovery_claim_at"] = time.time()
            record.pop("_stale_claim_floor", None)
            _atomic_json(handle._directory / "record.json", record)
            self._spawn_supervisor(handle, mode="reconnect", generation=generation)
            # The durable claim already prevents a second spawn. Do not wait for
            # the child to take supervisor.lock before returning — the claim is
            # the serialization mechanism. The child will take over (or the claim
            # expires after TTL for reclaim). Blocking here would add an unbounded
            # wait for the child's import/startup time.
        return handle

    def close(self) -> bool:
        """Kill the persistent sandbox. Returns True if killed, False otherwise.

        Governed like recover/terminate: the host declaration must prove
        authority over the existing sandbox (full typed identity match) and the
        frozen sandbox.json must belong to this owner, before any kill RPC. An
        unprovable authority or a foreign-owned sandbox returns False with zero
        side effects — an undeclared host must not kill another authority's
        sandbox.
        """
        if self._kill_sandbox is None:
            return False
        if not self._authority_proven():
            return False  # 0 side effects: cannot prove authority over this sandbox
        sandbox_file = self._sandbox_root / "sandbox.json"
        if not sandbox_file.is_file() or sandbox_file.is_symlink():
            return False
        try:
            sandbox_data = _json(sandbox_file)
        except Exception:
            return False
        # Ownership: the frozen sandbox.json must belong to this session's owner
        # key. A foreign-owned sandbox.json is not this session's sandbox.
        if sandbox_data.get("owner") != self.owner:
            return False
        sandbox_id = sandbox_data.get("sandbox_id", "")
        if not sandbox_id:
            return False
        self._kill_sandbox(sandbox_id)
        return True

    def terminate(self) -> str:
        """Terminate the owner's persistent sandbox with full status semantics.

        Returns one of: "terminated", "confirmed_absent", "not_e2b",
        "no_owner", "unknown", "failed", "timeout".

        Owner proof (what this proves, and its boundary):

        * **Account authority** — the typed connection target frozen in
          sandbox.json matches the host declaration in full (same
          connection_ref/tenant_ref/principal_ref/endpoint/auth_mode).
        * **Session ownership** — the owner key
          ``sha256([workspace, "e2b", session_id])`` ties the durable state
          (command records + sandbox.json) to the canonical ``session_id``
          supplied by the host (from ``session_service``).
        * **Sandbox↔command linkage** — when command records carry a
          ``sandbox_id`` (the production case: the supervisor writes it before
          sandbox.json), the sandbox.json ``sandbox_id`` must match one of
          them, proving the sandbox was launched by a command this session
          owns. A mismatch is inconsistent/tampered → unknown.

        This is host durable state, NOT the canonical launch receipt. Proving
        the ``sandbox_id`` corresponds to a canonical Native *run* requires the
        launch receipt (RunHandle/scope/receipt), which the read/stop
        ``BackgroundCommandLocator`` traverses; ``terminate`` operates at the
        session-durable-state level and does not re-traverse the receipt. The
        command record ``backend`` is metadata only (it distinguishes a
        deterministic ``not_e2b`` from a damaged ``unknown``); the first record
        never authorizes the kill. A frozen typed target with no matching host
        declaration, or a different typed identity, is ``unknown`` with zero
        backend calls — never a blind kill under a different authority.

        Ownership consistency (the owner key
        ``sha256([workspace, "e2b", session_id])`` ties the durable state to the
        canonical session id): every command record must be a VALID launch
        evidence record — well-formed JSON, owned by this owner
        (``rec.owner == self.owner``), ``command_id`` matching its directory,
        ``backend == "e2b"``, and a ``sandbox_id`` matching ``sandbox.json``'s.
        A corrupted record, a record missing ``sandbox_id`` or ``backend``, a
        foreign/tampered record, or a sandbox.json with no command records at
        all is missing/inconsistent launch evidence → ``unknown`` with zero
        backend calls. ``unknown`` retains metadata and never becomes
        ``confirmed_absent``. A shared typed account does NOT prove the sandbox
        was launched by this session. Valid-owner cleanup is preserved.
        """
        sandbox_file = self._sandbox_root / "sandbox.json"
        has_sandbox = sandbox_file.is_file() and not sandbox_file.is_symlink()
        # Scan all command records. Each must be a complete, valid launch
        # evidence record owned by this session. A record is a valid launch
        # evidence record iff: well-formed JSON; rec.owner == self.owner;
        # rec.command_id == its directory name; rec.backend == "e2b";
        # rec.sandbox_id present. Any deviation (corrupt JSON, foreign/tampered
        # owner or command_id, missing backend, missing sandbox_id) means that
        # record does not prove a launch this session owns → fail closed.
        commands_dir = self.directory
        has_commands = False
        inconsistent = False
        record_backends: set = set()
        matching_launch_evidence = False  # a record whose sandbox_id == sandbox.json's
        if commands_dir.is_dir():
            for child in sorted(commands_dir.iterdir()):
                rec_path = child / "record.json"
                if rec_path.is_file() and not rec_path.is_symlink():
                    has_commands = True
                    try:
                        rec = _json(rec_path)
                    except Exception:
                        # Corrupt record: cannot prove a launch → inconsistent.
                        inconsistent = True
                        continue
                    # Ownership + identity consistency.
                    if rec.get("owner") != self.owner or rec.get("command_id") != child.name:
                        inconsistent = True
                        continue
                    backend = rec.get("backend")
                    if backend is not None:
                        record_backends.add(backend)
                    # A launch evidence record for an e2b sandbox requires a
                    # backend of "e2b" and a sandbox_id. local_process is a
                    # legitimate non-e2b record (a different backend), not an
                    # inconsistent one — it is handled by the deterministic
                    # not_e2b judgement. Any other backend (unknown/foreign) or
                    # a missing sandbox_id on an e2b record means this record
                    # does not prove an e2b launch this session owns →
                    # inconsistent.
                    if backend == "e2b":
                        if not rec.get("sandbox_id"):
                            inconsistent = True
                            continue
                    elif backend != "local_process":
                        inconsistent = True
                        continue
        # No owner: no commands AND no sandbox.json.
        if not has_commands and not has_sandbox:
            return "no_owner"
        if not has_sandbox:
            # No frozen typed connection to prove authority. Records that all
            # prove a non-e2b backend are a deterministic not_e2b (allow delete);
            # anything else (e2b records without the frozen target, mixed
            # backends, no record backend, or a damaged record) → unknown. A
            # tampered/corrupt record makes the state inconsistent → unknown.
            if not inconsistent and record_backends == {"local_process"}:
                return "not_e2b"
            return "unknown"
        try:
            sandbox_data = _json(sandbox_file)
        except Exception:
            return "unknown"
        sandbox_id = sandbox_data.get("sandbox_id", "")
        if not sandbox_id:
            return "unknown"
        # Sandbox ownership: sandbox.json.owner must match this session's owner
        # key. A sandbox.json planted with a foreign owner is not this session's
        # sandbox → unknown, zero backend calls.
        if sandbox_data.get("owner") != self.owner:
            return "unknown"
        # Launch evidence: a sandbox.json with no command records means no command
        # this session owns recorded launching it → unknown. (Production always
        # writes the command record before sandbox.json.)
        if not has_commands:
            return "unknown"
        # Deterministic not_e2b: a sandbox.json with no typed identity (legacy/
        # damaged) whose command records are ALL local_process (no corrupt/
        # foreign/e2b record mixed in) is not an E2B sandbox at all — the
        # metadata can be deleted without a kill RPC. Judged before the
        # inconsistent check so a clean local_process-only session is
        # deterministically cleaned. A mixed state (local_process + a corrupt or
        # e2b record) is NOT deterministically not_e2b → falls through to unknown.
        frozen_target = self._frozen_target()
        if (frozen_target is None and not inconsistent
                and record_backends == {"local_process"}):
            return "not_e2b"
        # Any corrupt/foreign/tampered/incomplete record is inconsistent state
        # that cannot prove a clean launch → unknown.
        if inconsistent:
            return "unknown"
        # Sandbox↔command linkage: at least one valid launch evidence record must
        # carry a sandbox_id matching sandbox.json's. A sandbox.json pointing at
        # a sandbox no session command proves it launched → unknown, no kill.
        for child in sorted(commands_dir.iterdir()):
            rec_path = child / "record.json"
            if rec_path.is_file() and not rec_path.is_symlink():
                try:
                    rec = _json(rec_path)
                    if (rec.get("owner") == self.owner
                            and rec.get("command_id") == child.name
                            and rec.get("backend") == "e2b"
                            and rec.get("sandbox_id") == sandbox_id):
                        matching_launch_evidence = True
                        break
                except Exception:
                    pass
        if not matching_launch_evidence:
            return "unknown"
        # Reaching here means a valid e2b launch evidence record matched the
        # sandbox.json sandbox_id. A typed target was already required above
        # (frozen_target is not None here, or it would have returned at the
        # not_e2b/unknown judgement). Authority: the host declaration must
        # match the frozen typed identity in full. No declaration, or a
        # different tenant/principal at the same endpoint, is a different
        # authority → unknown, zero backend calls.
        if not self._authority_proven():
            return "unknown"
        if self._kill_sandbox is None:
            return "unknown"
        # Kill RPC in a bounded helper thread. SDK returns True=killed,
        # False=confirmed not found. Exception or timeout → unknown/failed.
        result_box: list = []
        def _do_kill():
            try:
                result_box.append(self._kill_sandbox(sandbox_id))
            except Exception:
                result_box.append(None)
        t = threading.Thread(target=_do_kill, daemon=True)
        t.start()
        t.join(timeout=_kill_rpc_timeout())
        if not result_box:
            return "timeout"
        if result_box[0] is True:
            return "terminated"
        if result_box[0] is False:
            return "confirmed_absent"
        return "failed"

    def _spawn_supervisor(
        self, handle: E2BBackgroundCommand, *, mode: str, generation: int = 0,
        request: dict[str, Any] | None = None,
    ) -> None:
        source_root = str(Path(__file__).resolve().parents[2])
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from ksadk.sandbox.e2b_background import _run_supervisor; _run_supervisor()"
        )
        config = {
            "mode": mode,
            "directory": str(handle._directory),
            "sandbox_root": str(self._sandbox_root),
            "owner": self.owner,
            "source_config": self._source_config,
            # Launch arguments travel only over the private stdin pipe. Recovery
            # reconnects by pid and never needs to persist command text or env.
            "command": (request or {}).get("command", ""),
            "cwd": (request or {}).get("cwd"),
            "env": (request or {}).get("env"),
            "timeout": handle._record().get("process_timeout_seconds", 0),
            "generation": generation,
        }
        try:
            process = subprocess.Popen(
                [sys.executable, "-B", "-I", "-c", script, source_root, str(handle._directory)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                # The supervisor imports trusted code; credentials arrive over
                # the stdin pipe, never via env or argv.
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            )
        except OSError:
            self._settle_unknown(handle, "supervisor spawn failed")
            return
        threading.Thread(target=process.wait, daemon=True).start()
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(config).encode())
            process.stdin.close()
        except OSError:
            # Return the stable locator even when acknowledgement is uncertain.
            try:
                process.stdin.close()
            except OSError:
                pass

    @staticmethod
    def _settle_unknown(handle: E2BBackgroundCommand, reason: str) -> None:
        record = handle._record()
        record.update(status="unknown", exit_code=None, completed_at=time.time(),
                       failure_reason=reason)
        _atomic_json(handle._directory / "record.json", record)


def _supervisor_lock_held(directory: Path) -> bool:
    """Cross-process liveness probe: True if a live supervisor holds the lock."""
    try:
        with os.fdopen(
            _open_private(directory / "supervisor.lock", os.O_RDWR | os.O_CREAT), "r+"
        ) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except OSError:
        return False
    return False


# Detached supervisor entry, kept here for existing process launchers.


def _run_supervisor() -> None:
    directory = Path(sys.argv[2])
    try:
        config = json.load(sys.stdin)
    except Exception:
        return
    try:
        source = _make_stream_source(
            config["source_config"], config.get("sandbox_id"), Path(config["sandbox_root"])
        )
        _supervise(directory, Path(config["sandbox_root"]), config, source)
    except Exception:
        logger.exception("E2B background supervisor crashed")
        try:
            _settle_unknown_safe(directory, "supervisor crashed")
        except Exception:
            pass


# Transport selection stays here so hosts can substitute a deterministic source.


def _make_stream_source(
    source_config: dict[str, Any], sandbox_id: str | None, sandbox_root: Path
) -> Any:
    # The stream transport is selected by ``stream`` (test-seam-only mock
    # transport) falling back to ``source_kind`` (the backend label). Production
    # never sets ``stream``; the test seam sets it to ``local_subprocess`` to run
    # the command as a local process while ``source_kind`` stays ``e2b`` so the
    # command record is truthfully labeled ``e2b`` (matching the sealed receipt
    # backend). This is a bounded mock-transport seam, not a production identity
    # override: the record backend, the receipt backend, and the verifier all
    # agree on ``e2b``; only the wire transport is mocked.
    kind = source_config.get("source_kind")
    stream = source_config.get("stream") or kind
    if stream == "local_subprocess":
        return _LocalSubprocessStreamSource(sandbox_id, source_config)
    if kind == "e2b" or stream == "e2b":
        return _E2BStreamSource(source_config, sandbox_id, sandbox_root)
    raise SandboxError(f"Unknown E2B background stream source kind: {kind!r}")


class E2BBackgroundSession:
    """Session wrapper exposing the background command lifecycle for E2B.

    Mirrors the subset of ``LocalProcessSandboxSession`` used by the product
    path: ``start_command`` / ``connect_background_command`` / ``sandbox_id``.
    """

    def __init__(
        self,
        *,
        store: E2BCommandStore,
        session_id: str,
    ):
        self._store = store
        self._session_id = session_id

    @classmethod
    def for_e2b(
        cls,
        *,
        state_root: Path,
        workspace_root: Path,
        session_id: str,
        sandbox_backend: Any,
        declaration: BackgroundHostDeclaration | None = None,
    ) -> "E2BBackgroundSession":
        store = E2BCommandStore(
            state_root=state_root,
            workspace_root=workspace_root,
            session_id=session_id,
            source_config=e2b_source_config(sandbox_backend, declaration),
            kill_sandbox=_e2b_kill_sandbox(sandbox_backend),
            declaration=declaration,
        )
        return cls(store=store, session_id=session_id)

    @classmethod
    def open(
        cls,
        *,
        state_root: Path,
        workspace_root: Path,
        session_id: str,
        source_config: dict[str, Any],
        kill_sandbox: Callable[[str], None] | None = None,
        declaration: BackgroundHostDeclaration | None = None,
    ) -> "E2BBackgroundSession":
        store = E2BCommandStore(
            state_root=state_root,
            workspace_root=workspace_root,
            session_id=session_id,
            source_config=source_config,
            kill_sandbox=kill_sandbox,
            declaration=declaration,
        )
        return cls(store=store, session_id=session_id)

    @property
    def store(self) -> E2BCommandStore:
        return self._store

    @property
    def sandbox_id(self) -> str:
        path = self._store._sandbox_root / "sandbox.json"
        return str(_json(path).get("sandbox_id") or "") if path.is_file() else ""

    def start_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        operation_key: str | None = None,
    ) -> E2BBackgroundCommand:
        if not str(command or "").strip():
            raise SandboxError("command is required")
        if timeout is not None and (type(timeout) is not int or timeout < 1):
            raise SandboxError("Background process timeout must be a positive integer")
        return self._store.start(
            command=command, cwd=cwd, env=env, timeout=timeout or 0, operation_key=operation_key,
        )

    def connect_background_command(self, command_id: str) -> E2BBackgroundCommand:
        # The product query path is also the recovery entry: if the prior
        # supervisor died mid-command, connecting attempts to reattach a
        # reconnect supervisor (never relaunching the command). This keeps
        # recovery on the same owned path as ordinary observation rather than
        # leaving it as an unused standalone helper.
        return self._store.connect_or_recover(command_id)

    def kill(self) -> bool:
        # A session-level kill must not destroy the persistent sandbox that may
        # still host running background commands for this owner.
        return False

    def terminate_owner(self) -> str:
        """Explicitly terminate the owner's persistent sandbox.

        Returns:
          "terminated" — sandbox killed (SDK True).
          "confirmed_absent" — SDK confirmed not found (SDK False).
          "not_e2b" — record backend is not e2b.
          "no_owner" — no command records and no sandbox.json.
          "unknown" — damaged/empty/legacy/mismatch.
          "failed" — kill RPC raised an exception.
          "timeout" — kill RPC did not return in time.
        Never called on ordinary aclose; only on explicit owner termination.
        """
        return self._store.terminate()
