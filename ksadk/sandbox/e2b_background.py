"""Host-owned durable background commands for the E2B sandbox backend.

This mirrors ``ksadk.sandbox.local_background``: a host-owned, file-backed
command store keyed by session owner, idempotent startup, UTF-8 output cursors,
cooperative cancellation and truthful unknown-outcome handling.

Durability is cross-process. Each command is owned by a **detached supervisor
subprocess** (``start_new_session=True``) that holds the remote E2B command's
event stream and appends output to host files. The supervisor survives the
tool/host process being killed; a new host process reads the same files and
checks the supervisor's cross-process lock for liveness. The persistent E2B
sandbox is owned by the supervisor and is **never** registered with the
disposable ``SandboxRegistry`` and **never** killed on atexit — killing it on
a normal exit would destroy commands awaiting recovery. Cleanup is an
explicit host action (``E2BCommandStore.close``); a host that dies without
cleaning up leaves the sandbox to the remote TTL (documented limitation).

The remote E2B command outlives any single client. The current implementation
and test stream (local_subprocess reconnect returns an empty stream) do not
prove that a disconnect gap is recoverable; the outcome is conservatively
reported as ``unknown`` when evidence is incomplete. The real deployed
service's replay semantics for commands.connect(pid) are not verified (e2b
2.21.1 only confirms ConnectRequest has no explicit output cursor; the envd
DataEvent.Fork replay behavior was not traced). Output already captured to
host files is preserved across reconnect; the gap, if any, is acknowledged
via ``evidence_incomplete`` and ``observed_exit_code`` rather than claimed
complete.
"""

from __future__ import annotations

import codecs
import fcntl
import hashlib
import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Protocol
from uuid import uuid4

from ksadk.sandbox.base import SandboxCommandResult, SandboxError

if TYPE_CHECKING:
    from ksadk.studio.native_background_connection import BackgroundHostDeclaration

logger = logging.getLogger(__name__)

_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "output_limit", "unknown"}
_COMMAND_ID = __import__("re").compile(r"[a-f0-9]{64}\Z")
# A recovery reservation is durable but not permanent: if the claiming host
# crashes before its supervisor takes over (or the spawn fails), a new host may
# reclaim after this TTL so an orphaned claim cannot block recovery forever.
_RECOVERY_CLAIM_TTL_SECONDS = 30.0

# ---------------------------------------------------------------------------
# File helpers (shared shape with local_background; duplicated to keep this
# module self-contained for the detached supervisor subprocess).
# ---------------------------------------------------------------------------


def _private_directory(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SandboxError("Background command state cannot follow symbolic links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _open_private(path: Path, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        with os.fdopen(_open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w") as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path: Path) -> dict[str, Any]:
    with os.fdopen(_open_private(path, os.O_RDONLY), "r") as source:
        result = json.load(source)
    if not isinstance(result, dict):
        raise SandboxError("Invalid background command record")
    return result


def _read_window(path: Path, cursor: int, max_bytes: int) -> tuple[str, int]:
    if type(cursor) is not int or cursor < 0:
        raise SandboxError("Command output cursor must be a non-negative byte offset")
    with os.fdopen(_open_private(path, os.O_RDONLY), "rb") as source:
        size = os.fstat(source.fileno()).st_size
        if cursor > size:
            raise SandboxError("Command output cursor exceeds retained output")
        source.seek(cursor)
        data = source.read(max_bytes)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        text = decoder.decode(data, final=False)
    except UnicodeDecodeError as exc:
        raise SandboxError("Command output cursor is not a UTF-8 boundary") from exc
    pending, _ = decoder.getstate()
    return text, cursor + len(data) - len(pending)


def _read_window_all(path: Path) -> tuple[str, int]:
    with os.fdopen(_open_private(path, os.O_RDONLY), "rb") as source:
        data = source.read()
    return data.decode("utf-8"), len(data)


# ---------------------------------------------------------------------------
# Command handle (read path) — cross-process liveness via supervisor.lock flock.
# ---------------------------------------------------------------------------


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


class BackgroundStream(Protocol):
    """Supervisor transport: iteration, observed terminal result and disconnect."""

    @property
    def result(self) -> Any: ...

    def __iter__(self) -> Iterator[tuple[str, str, str]]: ...

    def disconnect(self) -> None: ...


class _StreamAdapter:
    """Confine the E2B SDK's private result slot to one transport adapter."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    @property
    def result(self) -> Any:
        return getattr(self._stream, "_result", None)

    def __iter__(self):
        return iter(self._stream)

    @property
    def stop_check(self):
        return getattr(self._stream, "stop_check", None)

    @stop_check.setter
    def stop_check(self, callback):
        if hasattr(self._stream, "stop_check"):
            self._stream.stop_check = callback

    def disconnect(self) -> None:
        callback = getattr(self._stream, "disconnect", None)
        if callable(callback):
            callback()


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


# ---------------------------------------------------------------------------
# Command store (launch + recover + connect) — spawns detached supervisors.
# ---------------------------------------------------------------------------


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


# Bounded timeout for the kill RPC helper thread. This is our own implementation
# policy: a single SIGKILL RPC should return within seconds; if it hangs
# (network failure) the supervisor must not block indefinitely. Configurable
# via KSADK_E2B_KILL_TIMEOUT_SECONDS for hosts that need a different bound.
def _kill_rpc_timeout() -> float:
    raw = os.environ.get("KSADK_E2B_KILL_TIMEOUT_SECONDS", "10")
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return 10.0


def _safe_kill(source: Any, pid: int, kind: str) -> bool:
    """Issue a remote kill and return whether it was confirmed.

    A kill that raises, returns False, or cannot be confirmed within a bounded
    time is not a successful cancellation. The kill RPC runs in a helper thread
    with a bounded join so a non-returning RPC (network hang) does not block
    the watcher indefinitely.
    """
    result_box: list[bool] = []

    def _do_kill():
        try:
            result_box.append(bool(source.kill(pid)))
        except Exception:
            result_box.append(False)

    t = threading.Thread(target=_do_kill, daemon=True)
    t.start()
    t.join(timeout=_kill_rpc_timeout())
    return result_box[0] if result_box else False


def _disconnect_stream(stream: Any) -> None:
    """Close a command event stream without killing the command.

    The real E2B SDK CommandHandle (command_handle.py:88-95) provides disconnect()
    which closes the event generator without sending a signal. This lets the
    supervisor settle after a timeout even when the kill failed and the stream
    would otherwise block forever on ``for event in self._events``. For the
    local test harness (_PipeStream) disconnect is a no-op; stop_check handles
    the bounded exit there.
    """
    disconnect = getattr(stream, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:
            logger.warning("E2B background stream disconnect failed")


# ---------------------------------------------------------------------------
# Detached supervisor entry (runs in its own process).
# ---------------------------------------------------------------------------


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


def _settle_unknown_safe(directory: Path, reason: str) -> None:
    record = _json(directory / "record.json")
    record.update(status="unknown", exit_code=None, completed_at=time.time(),
                   failure_reason=reason, outcome_reason=reason)
    _atomic_json(directory / "record.json", record)


def _supervise(
    directory: Path, sandbox_root: Path, config: dict[str, Any], source: Any
) -> None:
    with os.fdopen(_open_private(directory / "supervisor.lock", os.O_RDWR), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = _json(directory / "record.json")
        mode = config["mode"]
        generation = int(config.get("generation") or 0)
        # All entry points first check the terminal state: a command that has
        # already settled must not be taken over again by any supervisor
        # (including a late start or a late reconnect child).
        if str(record.get("status", "unknown")) in _TERMINAL:
            logger.warning(
                "E2B background supervisor %s refused: record already terminal (%s)",
                directory.name, record.get("status"),
            )
            return
        record_generation = int(record.get("generation") or 0)
        # Generation fence (complete contract):
        # - reconnect: the parent persisted recovery_claim_generation before
        #   spawning this supervisor. If a different/newer claim exists, or the
        #   claim was already cleared but the record's generation advanced past
        #   this supervisor (a newer reclaim won), this supervisor is stale and
        #   must refuse to take over. It must never overwrite a newer record.
        # - start: a fresh command (generation 0) must not overwrite a record
        #   that already advanced to a higher generation via recovery.
        record_generation = int(record.get("generation") or 0)
        if mode == "reconnect":
            claimed = record.get("recovery_claim_generation")
            if claimed is not None:
                if int(claimed) != generation:
                    logger.warning(
                        "E2B background supervisor %s (gen=%d) superseded by claim gen=%s",
                        directory.name, generation, claimed,
                    )
                    return
            elif record_generation > generation:
                # Claim was cleared by a newer reclaim; this stale supervisor
                # must not overwrite the newer generation's record.
                logger.warning(
                    "E2B background supervisor %s (gen=%d) stale vs record gen=%d",
                    directory.name, generation, record_generation,
                )
                return
            # Confirm takeover: clear the claim and persist this generation.
            record["generation"] = generation
            record.pop("recovery_claim_generation", None)
            record.pop("recovery_claim_at", None)
        else:  # start
            if record_generation > generation:
                logger.warning(
                    "E2B background start supervisor %s refused: record gen=%d > %d",
                    directory.name, record_generation, generation,
                )
                return
            record["generation"] = generation
        cancel_path = directory / "cancel"
        record["supervisor_pid"] = os.getpid()
        # reconnect mode must not overwrite output already delivered to host
        # files; only append post-reconnect bytes. start mode is the first
        # supervisor and owns the canonical output.
        append_only = mode == "reconnect"
        try:
            if mode == "start":
                pid, stream, sandbox_id = source.start(
                    config["command"], cwd=config.get("cwd"),
                    envs=config.get("env"), timeout=config.get("timeout") or 0,
                )
                record.update(status="running", pid=pid, sandbox_id=sandbox_id,
                               started_at=time.time())
                _atomic_json(directory / "record.json", record)
                sandbox_json = {
                    "sandbox_id": sandbox_id, "owner": config["owner"],
                    "created_at": time.time(),
                }
                source_config = config.get("source_config") or {}
                # Freeze the typed host-declared connection target (non-sensitive
                # identity only; no key) as the canonical authority for later
                # read/stop/recover/terminate. A declaration that provided
                # connection_target wins; a legacy/test source that only carried
                # the transport triple falls back to that projection.
                typed = source_config.get("connection_target")
                if isinstance(typed, dict):
                    sandbox_json["connection"] = dict(typed)
                else:
                    conn = source_config.get("connection")
                    if isinstance(conn, dict):
                        sandbox_json["connection"] = {
                            "api_url": conn.get("api_url", ""),
                            "domain": conn.get("domain", ""),
                            "traffic_source": conn.get("traffic_source", ""),
                        }
                _atomic_json(sandbox_root / "sandbox.json", sandbox_json)
            else:  # reconnect
                pid = int(record.get("pid") or 0)
                stream = source.reconnect(pid, timeout=config.get("timeout") or 0)
                record["recovered_at"] = time.time()
                # The disconnect gap is acknowledged: prior captured output stays
                # in the host files; only post-reconnect output is appended. The
                # outcome stays unknown until a new terminal event is observed,
                # because E2B does not replay output produced during the gap.
                record["reconnect_gap"] = True
                _atomic_json(directory / "record.json", record)

            # Compute the remaining process deadline. A reconnect must not
            # reset the timeout: it preserves the original remaining window
            # measured from the command's original start, so a command that
            # already exhausted most of its deadline is not granted a fresh one.
            # None means no deadline (unlimited); 0 means the deadline is
            # already exhausted (must time out immediately), distinct from
            # unlimited so an expired command cannot run forever after reconnect.
            process_timeout = config.get("timeout") or 0
            if not process_timeout:
                remaining_seconds: float | None = None
            elif mode == "start":
                remaining_seconds = float(process_timeout)
            else:
                started_at = float(record.get("started_at") or 0)
                elapsed = max(0.0, time.time() - started_at) if started_at else 0.0
                remaining_seconds = max(0.0, float(process_timeout) - elapsed)

            # Cancellation and timeout are controlled by a watcher thread that
            # is independent of the stream's output events. The real E2B SDK
            # (command_handle.py:54-86) only yields on non-empty stdout/stderr/
            # pty and sets _result on end without yielding; a long silent command
            # blocks ``for event in self._events`` indefinitely, so a cancel
            # check placed after each yield would never run. The watcher polls
            # the cancel marker and the process deadline and issues the remote
            # kill directly.
            #
            # A kill request is separate from a confirmed terminal outcome. The
            # watcher records whether the kill RPC succeeded (returned True); the
            # final status is decided from the actually-observed result, not from
            # the request. A kill that was refused, failed, or whose effect is
            # unknown must not be reported as a successful cancellation.
            stream: BackgroundStream = _StreamAdapter(stream)
            cancel_requested = {"value": False}
            timeout_requested = {"value": False}
            kill_succeeded = {"value": False}
            watcher_done = threading.Event()
            consumed = threading.Event()

            def _watch() -> None:
                """Single arbiter for cancel/timeout and kill. The watcher is the
                only component that decides the deadline has expired or a cancel
                was requested, and the only one that issues kill. It also detects
                when the stream has received a terminal event (the SDK sets
                _result without yielding) so the supervisor can settle even if the
                RPC generator blocks after end. This avoids the race where
                consumer.join(timeout) and the watcher's own deadline check
                compete. After kill (success or failure), disconnect the stream
                so the consumer can settle, then return."""
                deadline = remaining_seconds
                start = time.monotonic()
                while True:
                    if not cancel_requested["value"] and cancel_path.exists():
                        cancel_requested["value"] = True
                        kill_succeeded["value"] = _safe_kill(source, pid, "cancel")
                        _disconnect_stream(stream)
                        return
                    if (deadline is not None and not timeout_requested["value"]
                            and time.monotonic() - start >= deadline):
                        timeout_requested["value"] = True
                        kill_succeeded["value"] = _safe_kill(source, pid, "timeout")
                        _disconnect_stream(stream)
                        return
                    # The SDK sets _result when an end event arrives but does not
                    # yield it, so the consumer's for-loop blocks on the next
                    # generator step. Detect _result here and signal the consumer
                    # to stop — the outcome is confirmed without blocking.
                    if stream.result is not None:
                        consumed.set()
                        watcher_done.set()
                        return
                    if watcher_done.wait(timeout=0.05):
                        return

            watcher = threading.Thread(target=_watch, name="e2b-bg-watch", daemon=True)
            watcher.start()

            def _should_stop_stream() -> bool:
                return (
                    cancel_requested["value"]
                    or timeout_requested["value"]
                    or stream.result is not None
                )

            if hasattr(stream, "stop_check"):
                stream.stop_check = _should_stop_stream

            result: Any = None
            stream_error = False
            settled = {"value": False}

            def _guarded_append(path: Path, text: str) -> None:
                # After the supervisor has committed a terminal status, late
                # output from a still-blocking consumer must not mutate the
                # host files or advance cursors.
                if settled["value"]:
                    return
                _append_output(path, text)

            def _consume():
                nonlocal result, stream_error
                try:
                    for out, err, _pty in stream:
                        if cancel_requested["value"] or timeout_requested["value"]:
                            break
                        if out:
                            _guarded_append(directory / "stdout", out)
                        if err:
                            _guarded_append(directory / "stderr", err)
                        if stream.result is not None:
                            break
                except Exception:
                    stream_error = True
                finally:
                    consumed.set()
                    watcher_done.set()

            consumer = threading.Thread(target=_consume, name="e2b-bg-stream", daemon=True)
            consumer.start()
            # Wait for the watcher (the single arbiter for cancel/timeout/kill).
            # The watcher returns when: (a) cancel is requested, (b) the deadline
            # expires, or (c) the consumer finished naturally (via watcher_done).
            # This means a natural completion exits immediately even with a
            # deadline — the process is not killed after it already finished.
            # Bounded by the process deadline if set; without a deadline, the
            # watcher exits on cancel or natural completion (both set watcher_done).
            if remaining_seconds is not None:
                watcher.join(timeout=max(remaining_seconds, 0.1) + 1)
            else:
                watcher.join()
            watcher_done.set()
            watcher.join(timeout=1)
            # After the arbiter has settled, give the consumer a bounded grace to
            # deliver any final result from the killed/disconnected stream.
            consumer.join(timeout=2)

            if not consumed.is_set():
                # The consumer is still blocking (kill failed, stream blocks on
                # RPC, disconnect did not help). The outcome is unknown.
                settled["value"] = True
                _settle_unknown_safe(directory, "stream blocked past deadline")
                return
            result = stream.result
            exit_code = getattr(result, "exit_code", None) if result is not None else None
            if stream_error and result is None:
                settled["value"] = True
                _settle_unknown_safe(directory, "stream ended without result")
                return
            # Decide status from the observed result, keeping the real exit code.
            # A cancel/timeout request that the kill could not confirm does not
            # invent a terminal outcome.
            if exit_code is not None:
                # A terminal event was observed: report the real exit code. If a
                # cancel/timeout was requested, classify the reason but keep the
                # observed code rather than overwriting it. A cancel whose kill
                # was refused does not turn a successful natural exit into failed;
                # the command finished on its own.
                if cancel_requested["value"] and kill_succeeded["value"]:
                    status, reason = "cancelled", "cancelled"
                elif cancel_requested["value"] and not kill_succeeded["value"]:
                    # Cancel was requested but the kill was refused; the command
                    # finished naturally. Report its real outcome, not a fake
                    # cancellation or a blanket failed.
                    if exit_code == 0:
                        status, reason = "completed", "exit_after_cancel_request"
                    else:
                        status, reason = "failed", "exit_after_cancel_request"
                elif timeout_requested["value"] and kill_succeeded["value"]:
                    status, reason = "timed_out", "timed_out"
                elif timeout_requested["value"] and not kill_succeeded["value"]:
                    if exit_code == 0:
                        status, reason = "completed", "exit_after_timeout_request"
                    else:
                        status, reason = "failed", "exit_after_timeout_request"
                elif exit_code == 0:
                    status, reason = "completed", ""
                else:
                    status, reason = "failed", ""
            elif cancel_requested["value"] and kill_succeeded["value"]:
                # No terminal event, but the kill succeeded: the process was
                # cancelled. Exit code is unknown (killed).
                status, exit_code, reason = "cancelled", None, "killed_by_cancel"
            elif timeout_requested["value"] and kill_succeeded["value"]:
                # Kill succeeded but no terminal event was observed (stream was
                # disconnected). The exit code is unknown (killed), not forged as
                # 124. The status is timed_out with a classified reason.
                status, exit_code, reason = "timed_out", None, "killed_by_timeout"
            elif cancel_requested["value"] or timeout_requested["value"]:
                # A kill was requested but could not be confirmed: the remote
                # outcome is unknown, not a successful cancel/timeout.
                status, exit_code, reason = "unknown", None, "kill_unconfirmed"
            else:
                status, exit_code, reason = "unknown", None, "no_terminal_event"
            # In reconnect mode the host files already hold the pre-gap output;
            # do not replace them. In start mode the append log is the complete
            # output, so finalize it (replace) for cursor stability.
            if not append_only:
                for channel in ("stdout", "stderr"):
                    _finalize_output_from_log(directory / channel)
            record = _json(directory / "record.json")
            # Terminal-state generation fence: if a newer recovery advanced the
            # record's generation past this supervisor while it held the lock is
            # impossible (flock is exclusive), but defend anyway — a stale
            # supervisor that reacquired the lock after a newer reclaim must not
            # overwrite the newer generation's terminal result.
            if int(record.get("generation") or 0) > generation:
                logger.warning(
                    "E2B background supervisor %s refused terminal write: record gen=%d > %d",
                    directory.name, int(record.get("generation") or 0), generation,
                )
                settled["value"] = True
                return
            # A reconnect that observed a terminal event still cannot claim a
            # complete outcome: output from the disconnect gap is missing, so
            # the evidence is incomplete even if the exit code is known. Keep
            # the exit code for inspection but mark the outcome unknown.
            if append_only and record.get("reconnect_gap"):
                record["evidence_incomplete"] = True
                record["observed_exit_code"] = exit_code
                record["observed_reason"] = reason
                status = "unknown"
                exit_code = None
            else:
                record["outcome_reason"] = reason
            # Mark settled before writing the terminal record so any late output
            # from the still-blocking consumer is rejected by _guarded_append.
            settled["value"] = True
            record.update(status=status, exit_code=exit_code, completed_at=time.time())
            _atomic_json(directory / "record.json", record)
        except Exception:
            _settle_unknown_safe(directory, "supervisor body failed")
            raise


def _append_output(path: Path, text: str) -> None:
    with os.fdopen(_open_private(path, os.O_WRONLY | os.O_APPEND), "ab") as out:
        out.write(text.encode("utf-8"))


def _finalize_output_from_log(path: Path) -> None:
    """Atomically replace the append log with itself, making the retained
    output the canonical record for cursor stability. The content is unchanged;
    only the in-place append log is committed as the durable full output."""
    with os.fdopen(_open_private(path, os.O_RDONLY), "rb") as source:
        data = source.read()
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    with os.fdopen(_open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Stream sources — pluggable so the lifecycle can be exercised without a real
# remote sandbox. Production uses ``e2b``; the ``local_subprocess`` source runs
# the command as a local process and is a deterministic lifecycle harness.
# ---------------------------------------------------------------------------


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


class _E2BStreamSource:
    """Real E2B stream source, constructed inside the supervisor process."""

    def __init__(
        self, source_config: dict[str, Any], sandbox_id: str | None, sandbox_root: Path
    ) -> None:
        from ksadk.sandbox.backends.e2b import E2BSandboxBackend
        from ksadk.sandbox.base import SandboxSpec, SandboxType
        from ksadk.sandbox.e2b_connection import ExplicitE2BConnection

        spec = source_config["spec"]
        spec_obj = SandboxSpec(
            template_id=spec["template_id"],
            sandbox_type=SandboxType(spec["sandbox_type"]),
            timeout=spec["timeout"],
            allow_internet_access=spec["allow_internet_access"],
            metadata=dict(spec.get("metadata") or {}),
            env=dict(spec.get("env") or {}),
        )
        conn = source_config.get("connection")
        conn_obj = None
        if conn is not None:
            conn_obj = ExplicitE2BConnection(
                api_url=conn["api_url"], domain=conn["domain"],
                api_key=conn["api_key"], traffic_source=conn["traffic_source"],
            )
        self._source_config = source_config
        self._backend = E2BSandboxBackend(spec=spec_obj, connection=conn_obj)
        self._sandbox_id = sandbox_id
        self._sandbox_root = sandbox_root
        self._session: Any = None

    def _session_get(self) -> Any:
        if self._session is not None:
            return self._session
        # Serialize sandbox get-or-create across concurrent supervisors.
        with os.fdopen(
            _open_private(self._sandbox_root / "sandbox.lock", os.O_RDWR | os.O_CREAT), "r+"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            sandbox_file = self._sandbox_root / "sandbox.json"
            if (
                self._sandbox_id is None
                and sandbox_file.is_file()
                and not sandbox_file.is_symlink()
            ):
                self._sandbox_id = _json(sandbox_file).get("sandbox_id") or None
            if self._sandbox_id:
                self._session = self._backend.reconnect_session(session_locator=self._sandbox_id)
            else:
                self._session = self._backend.create_session(
                    session_id=self._sandbox_root.name, input_files=[]
                )
                self._sandbox_id = getattr(self._session, "sandbox_id", "") or ""
                if not self._sandbox_id:
                    raise SandboxError("E2B background sandbox did not expose a sandbox id")
                identity = {"sandbox_id": self._sandbox_id, "owner": self._sandbox_root.name,
                            "created_at": time.time()}
                connection = (self._source_config.get("connection_target")
                              or self._source_config.get("connection"))
                if isinstance(connection, dict):
                    identity["connection"] = {key: value for key, value in connection.items()
                                              if key != "api_key"}
                # Publish identity before releasing the get-or-create lock; a
                # second supervisor must reconnect even before start() returns.
                _atomic_json(sandbox_file, identity)
        return self._session

    def start(self, command, *, cwd, envs, timeout):
        session = self._session_get()
        handle = session.start_background_stream(
            command, envs=envs, cwd=cwd, timeout=max(timeout, 0) or None,
        )
        pid = int(getattr(handle, "pid", 0) or 0)
        if pid <= 0:
            raise SandboxError("E2B background command did not expose a process id")
        return pid, handle, self._sandbox_id

    def reconnect(self, pid, *, timeout):
        session = self._session_get()
        return session.connect_background_stream(pid, timeout=timeout or None)

    def kill(self, pid):
        return bool(self._session_get().kill_command(pid))


class _LocalSubprocessStreamSource:
    """Deterministic lifecycle harness: the "remote command" is a local process.

    Used to exercise the detached-supervisor lifecycle (cross-process survival,
    idempotency, cancellation, unknown outcome) without a real E2B sandbox.
    The command runs as a child of the supervisor, so it survives the tool/host
    process being killed while the supervisor lives. Like E2B's event stream,
    the pipe does not replay output: once the supervisor dies the buffered
    output is lost, so the outcome is ``unknown`` (no replay), faithfully.
    """

    def __init__(self, sandbox_id: str | None, source_config: dict[str, Any] | None = None) -> None:
        self._sandbox_id = sandbox_id or "fake-local-subprocess"
        # Test knob: "fail" makes kill return False (refused); "raise" makes it
        # throw (unknown). Defaults to True (kill succeeds). Production e2b
        # never uses local_subprocess, so this never affects real backends.
        self._kill_mode = (source_config or {}).get("kill_succeeds", True)

    def start(self, command, *, cwd, envs, timeout, sandbox_root=None):
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, cwd=cwd or None, env=envs or None,
        )
        return proc.pid, _PipeStream(proc), self._sandbox_id

    def reconnect(self, pid, *, timeout):
        # Cannot reattach to the original pipe; E2B does not replay output.
        return _PipeStream(None)

    def kill(self, pid):
        if self._kill_mode == "raise":
            raise OSError("injected kill failure")
        if self._kill_mode == "fail":
            return False
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False


class _PipeStream:
    """Iterable over a local subprocess stdout/stderr, mirroring the SDK handle."""

    def __init__(self, proc: subprocess.Popen | None):
        self._proc = proc
        self._result: Any = None
        # Optional callback: () -> bool. When set, the stream polls it on each
        # select timeout and stops (without yielding) when it returns True. This
        # lets the supervisor exit when a kill could not be confirmed rather than
        # blocking forever on a still-running process. Mirrors the SDK shape:
        # no idle tick is yielded; the stream simply stops.
        self.stop_check: Callable[[], bool] | None = None

    @property
    def pid(self) -> int:
        return self._proc.pid if self._proc is not None else 0

    def __iter__(self):
        if self._proc is None:
            return
        stdout = self._proc.stdout
        stderr = self._proc.stderr
        fds: list[int] = []
        fd_channel: dict[int, str] = {}
        if stdout is not None:
            fds.append(stdout.fileno())
            fd_channel[stdout.fileno()] = "stdout"
        if stderr is not None:
            fds.append(stderr.fileno())
            fd_channel[stderr.fileno()] = "stderr"
        # Mirror the real E2B SDK (command_handle.py:54-86): yield only on
        # non-empty stdout/stderr and set _result on process end without
        # yielding. A silent long command blocks here; cancellation is handled
        # by the independent watcher thread, not by polling after a yield. The
        # select timeout only lets the supervisor notice a stop_check (e.g. an
        # unconfirmed kill) — it never yields an idle tick.
        stopped = False
        while fds:
            try:
                ready, _, _ = select.select(fds, [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                if self.stop_check is not None and self.stop_check():
                    stopped = True
                    break
                continue
            for fd in ready:
                data = os.read(fd, 65536)
                if not data:
                    fds = [f for f in fds if f != fd]
                    continue
                text = data.decode("utf-8", "replace")
                if fd_channel.get(fd) == "stdout":
                    yield text, None, None
                else:
                    yield None, text, None
        if stopped:
            # The supervisor asked us to stop (timeout deadline reached). If the
            # kill succeeded the process is dead and wait() returns immediately
            # with a real exit code. If the kill failed the process is still
            # alive; wait() times out (0.5s, bounded by the select poll interval)
            # and _result stays unset so the supervisor reports unknown with
            # kill_unconfirmed. This is not an arbitrary grace — it is the time
            # for a SIGKILL to take effect, which is near-instant.
            try:
                self._proc.wait(timeout=0.5)
                self._result = _Result(self._proc.returncode)
            except subprocess.TimeoutExpired:
                pass
            return
        self._proc.wait()
        self._result = _Result(self._proc.returncode)


class _Result:
    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Session wrapper (product-path surface).
# ---------------------------------------------------------------------------


def e2b_source_config(
    backend: Any, declaration: BackgroundHostDeclaration | None = None,
) -> dict[str, Any]:
    """Build the serializable supervisor config from an ``E2BSandboxBackend``.

    When a host declaration is present, its non-sensitive typed
    ``connection_target`` is carried alongside the credential-bearing
    ``connection`` so the supervisor freezes the canonical authority into
    sandbox.json at launch. The key stays in ``connection`` (private, over the
    stdin pipe only) and is never written to sandbox.json.
    """
    from ksadk.sandbox.base import SandboxType  # noqa: F401

    spec = backend.spec
    connection = backend.connection
    config: dict[str, Any] = {
        "source_kind": "e2b",
        "spec": {
            "template_id": spec.template_id,
            "sandbox_type": spec.sandbox_type.value,
            "timeout": spec.timeout,
            "allow_internet_access": spec.allow_internet_access,
            "metadata": dict(spec.metadata),
            "env": dict(spec.env),
        },
        "connection": (
            {
                "api_url": connection.api_url,
                "domain": connection.domain,
                "api_key": connection.api_key.get_secret_value(),
                "traffic_source": connection.traffic_source,
            }
            if connection is not None
            else None
        ),
    }
    if declaration is not None:
        config["connection_target"] = declaration.target.to_dict()
    return config


def _e2b_kill_sandbox(backend: Any) -> Callable[[str], bool]:
    def kill(sandbox_id: str) -> bool:
        session = backend.reconnect_session(session_locator=sandbox_id)
        return bool(session.kill())

    return kill


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
