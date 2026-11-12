"""Host-owned idle wake for ordinary Native background commands.

After an ordinary Native run terminates idle while one of its owned local background
commands is still running, this coordinator detects the command's *real* terminal
status and admits a dedicated background-notification turn through the authoritative
Native adapter, so the completion notice enters the next model request in the same
session and task chain.

Coordinator contract:

- One Studio service process owns each workspace records file. Separate Studio
  processes must use separate workspace roots; engine CAS is not a file lock.

- No fixed wake cap. Storm control is *real* terminal status plus *durable* per-task
  outcome records (each task_id resolved exactly once, persisted). The number of wake
  turns is bounded by the number of actually launched commands.

- Does not infer terminal == notified. All launched task_ids are watched; the
  adapter's ``notified_task_ids`` is the authoritative filter for already-delivered
  commands, and the kernel's ``command_probe`` skips already-notified entries in the
  wake turn.

- Three durable outcomes, each audited, none masquerading as the other:
    ``admitted``       — the notification turn was created (or already existed idempotently).
    ``pending_admission`` — a transient precondition failed (session active, adapter
                          error); recorded as a fact, the watch is retained for retry.
    ``discarded``      — a permanent precondition failed (already notified, source
                          canceled); recorded as a durable, auditable fact.
  Transient contention (session has an active turn) retains the watch for retry.

- The wake goes through the adapter's ``start_notification_turn`` — a controlled
  admission under the same scope/configuration/Build identity — never a synthesized
  user goal. Does not revive a terminal run, poll as user input, reset budgets, or
  reuse any unconfirmed effect.

- Cold restart IS supported, for every unresolved watch. Registration happens
  in-process at run completion and durably persists a ``watching`` record (fsync)
  carrying the full command fields, so a process kill (no ``stop``) does not lose
  the watch. On construction the coordinator re-registers every non-terminal
  record (``watching``, ``pending_admission`` and ``admitted``) from the records
  file and restarts the poll loop from the formal async service entry (``start``).
  Only ``discarded`` is a terminal dedup and is never re-fired. ``admitted`` is
  NOT terminal: it means the notification turn's admission committed to the
  durable Native store, but the in-process driver may not have advanced the turn
  to the model boundary (a crash window between the durable commit and the driver
  finishing, including after the observation was staged at PREPARING_CONTEXT but
  before the model intent). An admitted watch is rebuilt and re-fired; the
  engine re-attaches the existing turn idempotently and re-drives it. If the turn
  was already delivered (a prior terminal turn), the engine's request-identity
  check or already-notified guard makes it a durable discard — no duplicate model
  request. ``pending_admission`` is the durable reason for a retry. This is
  bounded retry, not revival: a terminal source stays terminal, the adapter's
  ``start_notification_turn`` re-validates scope/session/Build identity and
  rejects a canceled source or a changed admission request, and budgets are
  never reset. Carry-over of an already-delivered command uses the kernel's
  authoritative ``notified_task_ids`` filter. A persist failure at registration
  is surfaced (raised) to the caller rather than silently dropping the watch.

- Durable outcome records use a multi-dimensional key (scope, source_run, task) and
  are not capped/truncated. A missing records file on first start is fine; an
  existing but corrupt/unreadable/type-invalid file is a hard failure that blocks
  the coordinator (raises), not a silent reset to empty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_TERMINAL = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "output_limit", "unknown"}
)

_OUTCOME_ADMITTED = "admitted"
_OUTCOME_PENDING = "pending_admission"
_OUTCOME_DISCARDED = "discarded"
# A watch registered but not yet resolved (command still running, or terminal
# but the session was busy so admission has not been attempted). Non-terminal:
# rebuilt on cold restart so the watch is not lost. Distinct from pending_admission,
# which means admission was attempted and the dispatcher returned pending.
_OUTCOME_WATCHING = "watching"
_DURABLE_OUTCOMES = frozenset({
    _OUTCOME_ADMITTED, _OUTCOME_DISCARDED, _OUTCOME_PENDING, _OUTCOME_WATCHING,
})
# Only ``discarded`` is a true terminal dedup: already-notified, source-canceled,
# exhausted budget or task-not-found are permanent — a re-fire can never deliver.
# ``admitted`` is NOT terminal: it means the notification turn's admission
# committed to the durable Native store (``create_notification_turn`` is
# idempotent by run id), but the in-process driver that advances the turn to the
# model boundary may not have completed — the process can exit between the
# durable commit and the driver scheduling/finishing (a crash window). On cold
# restart an admitted watch is rebuilt and re-fired; the engine's
# ``start_notification_turn`` re-attaches the existing turn idempotently and
# ``register_and_drive_notification_turn`` re-drives it, so a not-yet-delivered
# admission is driven to completion. If the turn was already delivered, the
# engine's authoritative ``already_notified`` pre-check raises StoreConflict and
# the dispatcher classifies it as ``discarded`` — no duplicate model request. So
# admitted is a recoverable, non-terminal outcome; the store's already-notified
# guard (not this record) is the delivery authority.
_TERMINAL_OUTCOMES = frozenset({_OUTCOME_DISCARDED})
# Outcomes rebuilt on cold restart (non-terminal): a watch in any of these
# states may still need to drive the notification to the model boundary.
_RECOVERABLE_OUTCOMES = frozenset({
    _OUTCOME_ADMITTED, _OUTCOME_PENDING, _OUTCOME_WATCHING,
})


class WakeDispatcher:
    """Controlled admission surface the coordinator calls.

    Implemented by the Studio RunService binding to the NativeRuntimeAdapter.
    ``admit`` returns one of: ``"admitted"`` (turn created or idempotent),
    ``"discarded"`` (permanent rejection, e.g. already notified / canceled), or
    ``"pending"`` (transient, retry later). ``notified_task_ids`` and
    ``is_source_canceled`` are authoritative queries against the Native ledger.
    """

    async def admit(
        self, *, scope_id: str, source_run_id: str, task_id: str,
        bundle_digest: str, session_id: str,
        build_id: str = "", model: str = "", approval_mode: str = "",
    ) -> str: ...

    def notified_task_ids(
        self, source_run_id: str, bundle_digest: str, session_id: str,
    ) -> set[str]: ...

    def is_source_canceled(
        self, source_run_id: str, bundle_digest: str, session_id: str,
    ) -> bool: ...

    def release_notification_lease(
        self, source_run_id: str, task_id: str, bundle_digest: str, session_id: str,
    ) -> None:
        """Release an admitted-but-not-driven turn's stale lease on shutdown."""
        ...


# A status reader returns the command's current status string, or raises on a
# transient read error.
StatusReader = Callable[
    [str, str, Path, Path, str], Awaitable[str]
]  # (task_id, session_id, workspace_root, command_state_root, studio_run_id) -> status
IsIdle = Callable[[str, str], bool]  # (agent_id, session_id) -> no active turn


@dataclass
class _PendingCommand:
    scope_id: str
    session_id: str
    build_id: str
    agent_id: str
    source_run_id: str
    native_run_id: str | None
    task_id: str
    bundle_digest: str
    workspace_root: Path
    command_state_root: Path
    studio_run_id: str = ""
    # Source Build resolution fields so a cold-restart dispatcher can reopen the
    # Native activation by resolving the sealed Build (model + approval_mode)
    # without a new user run. Empty when the host did not supply them; the
    # dispatcher then cannot reopen and the watch stays pending.
    model: str = ""
    approval_mode: str = ""
    retry_attempts: int = 0
    retry_after: float = 0.0

    def key(self) -> tuple[str, str, str]:
        return (self.scope_id, self.source_run_id, self.task_id)


class NativeBackgroundWakeup:
    """Coordinate idle completion wake for owned local background commands."""

    def __init__(
        self,
        *,
        status_reader: StatusReader,
        is_idle: IsIdle,
        dispatcher: WakeDispatcher,
        records_path: Path,
        poll_interval: float = 0.5,
    ) -> None:
        self._status_reader = status_reader
        self._is_idle = is_idle
        self._dispatcher = dispatcher
        self._records_path = records_path
        # One Studio writer owns a workspace. Serialize worker-thread persistence
        # with synchronous registration so writes cannot overwrite each other.
        self._record_lock = threading.RLock()
        self._poll_interval = max(0.05, float(poll_interval))
        self._pending: dict[tuple[str, str, str], _PendingCommand] = {}
        self._firing: set[tuple[str, str, str]] = set()
        self._firing_sessions: set[tuple[str, str]] = set()
        self._checks: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self._records: dict[tuple[str, str, str], dict[str, Any]] = self._load_records()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Cold restart recovery: re-register every non-terminal watch from the
        # durable records file so it is retried under this process's
        # status_reader/dispatcher. Only ``discarded`` is terminal (a permanent
        # dedup, never re-added). ``watching``, ``pending_admission`` and
        # ``admitted`` are recoverable: ``admitted`` means the notification turn's
        # admission committed to the durable Native store but the in-process
        # driver may not have advanced it to the model boundary (a crash window,
        # including after the observation was staged at PREPARING_CONTEXT but
        # before the model intent). A non-terminal record carries the full
        # command fields, so the watch is rebuilt exactly — no synthesized fields.
        # The poll loop is started here only when a running event loop already
        # exists (direct async construction, e.g. unit tests); a sync
        # constructor — StudioService is built without a running loop — only
        # queues the watches, and ``start()`` (called from the formal async
        # service entry) starts the poll. ``register()`` also starts the poll for
        # in-process registrations.
        self._recover_pending_from_records()
        if self._pending:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                self._ensure_poll()

    async def start(self) -> None:
        """Start the poll loop from the formal async service lifecycle entry.

        ``StudioService.start()`` calls this after recovering interrupted runs.
        Construction only queues recovered watches (no running loop); this is
        where they begin to fire. Idempotent. A stopped coordinator is sealed;
        recovery belongs to a new service instance.
        """
        if self._pending:
            self._ensure_poll()

    def _recover_pending_from_records(self) -> None:
        # Rebuild every non-terminal watch into the in-memory pending set so it is
        # retried under this process. Recoverable outcomes:
        #   watching         — registered, not yet resolved (command running, or
        #                       terminal but the session was busy).
        #   pending_admission — admission attempted, dispatcher returned pending.
        #   admitted          — admission committed to the durable Native store, but
        #                       the in-process driver may not have advanced the turn
        #                       to the model boundary (a crash window between the
        #                       durable commit and the driver finishing). Re-firing
        #                       re-attaches the existing turn idempotently and
        #                       re-drives it; if it was already delivered, the
        #                       engine's already-notified guard makes it a discard.
        # Only ``discarded`` is terminal and not rebuilt.
        for key, record in list(self._records.items()):
            if record.get("outcome") not in _RECOVERABLE_OUTCOMES:
                continue
            if key in self._pending:
                continue
            try:
                self._pending[key] = _PendingCommand(
                    scope_id=str(record["scope_id"]),
                    session_id=str(record["session_id"]),
                    build_id=str(record.get("build_id") or ""),
                    agent_id=str(record.get("agent_id") or str(record["scope_id"])),
                    source_run_id=str(record["source_run_id"]),
                    native_run_id=record.get("native_run_id"),
                    task_id=str(record["task_id"]),
                    bundle_digest=str(record.get("bundle_digest") or ""),
                    workspace_root=Path(str(record.get("workspace_root") or "")),
                    command_state_root=Path(str(record.get("command_state_root") or "")),
                    studio_run_id=str(record.get("studio_run_id") or ""),
                    model=str(record.get("model") or ""),
                    approval_mode=str(record.get("approval_mode") or ""),
                    retry_attempts=int(record.get("retry_attempts") or 0),
                    retry_after=float(record.get("retry_after") or 0),
                )
            except (KeyError, TypeError, ValueError):
                # A non-terminal record missing the fields needed to rebuild the
                # watch cannot be retried safely. Drop it from recovery (it
                # remains in the durable audit file) rather than fire a half-built
                # watch. This loss is logged, not silent.
                logger.warning(
                    "background_wakeup.recover_skip key=%s missing fields", key
                )

    async def register(
        self,
        *,
        scope_id: str,
        session_id: str,
        build_id: str,
        agent_id: str,
        source_run_id: str,
        native_run_id: str | None,
        task_id: str,
        bundle_digest: str,
        workspace_root: Path,
        command_state_root: Path,
        studio_run_id: str = "",
        model: str = "",
        approval_mode: str = "",
    ) -> None:
        key = (scope_id, source_run_id, task_id)
        existing = self._records.get(key)
        if existing is not None and existing.get("outcome") in _TERMINAL_OUTCOMES:
            return  # already resolved durably (discarded — permanent dedup)
        # A watching/pending/admitted outcome is not terminal: a cold restart
        # re-registers the watch so it can be retried (the record is the durable
        # reason for the retry, not a permanent resolution).
        if key in self._firing or key in self._pending:
            return
        command = _PendingCommand(
            scope_id=scope_id, session_id=session_id, build_id=build_id,
            agent_id=agent_id, source_run_id=source_run_id,
            native_run_id=native_run_id, task_id=task_id,
            bundle_digest=bundle_digest,
            workspace_root=workspace_root, command_state_root=command_state_root,
            studio_run_id=studio_run_id, model=model, approval_mode=approval_mode,
        )
        # Durably persist the watch at registration (fsync) so a process kill
        # (no stop()) does not lose it. A persist failure is surfaced (raised)
        # rather than silently dropping the watch: the caller logs it and the
        # run result is unaffected (registration is best-effort after COMPLETED).
        if not self._record_outcome(command, _OUTCOME_WATCHING):
            raise RuntimeError(
                "background_wakeup: watch persist failed; not durable "
                f"(task_id={task_id})"
            )
        self._pending[key] = command
        self._ensure_poll()

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        # Release stale leases for admitted-but-not-driven notification turns so
        # a restart can re-drive them without waiting for the lease TTL. An
        # admitted turn committed to the durable store but whose driver never
        # finished holds an orphaned lease; the facade skips runs with an active
        # driver. Best effort: a missing adapter (activation already closed) is
        # fine — the lease expires on its TTL. Do this before clearing _pending so
        # the commands' identity (source_run_id/task_id/bundle/session) are still
        # available; admitted watches are already popped from _pending, so iterate
        # the durable records. Public projection awaits the notification's
        # execution before admit() returns, so an already-created notification
        # may still have a watching/pending outcome here. The facade resolves
        # the deterministic notification identity and releases only a lease it
        # owns with no active driver; this never releases the source run.
        for key, record in list(self._records.items()):
            if record.get("outcome") not in _RECOVERABLE_OUTCOMES:
                continue
            try:
                self._dispatcher.release_notification_lease(
                    str(record["source_run_id"]), str(record["task_id"]),
                    str(record.get("bundle_digest") or ""),
                    str(record.get("session_id") or ""),
                )
            except Exception as exc:
                logger.debug(
                    "background_wakeup.release_lease_failed task_id=%s err=%s",
                    record.get("task_id"), type(exc).__name__,
                )
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._pending.clear()
        self._firing.clear()
        self._firing_sessions.clear()

    def pending_count(self) -> int:
        return len(self._pending)

    def outcome(self, scope_id: str, source_run_id: str, task_id: str) -> str | None:
        record = self._records.get((scope_id, source_run_id, task_id))
        return str(record["outcome"]) if record is not None else None

    # -- internals -------------------------------------------------------

    def _ensure_poll(self) -> None:
        # Late foreground completion can register a durable watch while the
        # service awaits teardown. Never dispatch into a closing execution host;
        # the next instance recovers the watch from disk.
        if self._stop.is_set():
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        try:
            while (self._pending or self._checks) and not self._stop.is_set():
                for key, check in tuple(self._checks.items()):
                    if check.done():
                        self._checks.pop(key)
                        if not check.cancelled() and (error := check.exception()) is not None:
                            logger.warning("background_wakeup.check_error err=%s",
                                           type(error).__name__)
                # A notification may wait for model output or user approval.
                # Keep polling other watches, including newly registered ones.
                # A watch has one checker; admission below also serializes each
                # agent/session without blocking independent sessions.
                for key in tuple(self._pending):
                    if key not in self._checks:
                        self._checks[key] = asyncio.create_task(self._check(key))
                if not self._pending and not self._checks:
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("background_wakeup.poll_loop_error err=%s", type(exc).__name__)
        finally:
            checks = tuple(self._checks.values())
            for check in checks:
                if not check.done():
                    check.cancel()
            if checks:
                await asyncio.gather(*checks, return_exceptions=True)
            self._checks.clear()

    async def _check(self, key: tuple[str, str, str]) -> None:
        cmd = self._pending.get(key)
        if cmd is None or time.time() < cmd.retry_after:
            return
        try:
            status = await self._status_reader(
                cmd.task_id, cmd.session_id, cmd.workspace_root,
                cmd.command_state_root, cmd.studio_run_id,
            )
        except Exception as exc:
            logger.debug(
                "background_wakeup.read_retry task_id=%s err=%s",
                cmd.task_id, type(exc).__name__,
            )
            self._defer(cmd)
            return
        if status not in _TERMINAL:
            return
        await self._fire(cmd)

    async def _fire(self, cmd: _PendingCommand) -> None:
        if self._stop.is_set():
            return
        key = cmd.key()
        if key in self._firing:
            return
        # Only a ``discarded`` durable outcome is a terminal dedup (never re-fire):
        # already-notified, source-canceled, exhausted budget or task-not-found are
        # permanent. ``admitted`` is NOT terminal — it means the notification turn's
        # admission committed to the durable Native store, but the in-process
        # driver may not have advanced it to the model boundary (a crash window,
        # including after the observation was staged at PREPARING_CONTEXT but before
        # the model intent). A re-fire (after cold reload or re-register) re-attaches
        # the existing turn idempotently — R4 attaches the existing run BEFORE the
        # already-notified precheck, so an unfinished turn is re-driven rather than
        # discarded. A turn that already reached terminal (model completed) is
        # re-attached but its driver is a no-op — no new model HTTP is dispatched
        # (exactly-once delivery). The dispatcher's ``notified_task_ids`` precheck
        # below may also discard a task the kernel ledger marks already delivered.
        # So an admitted record is re-fired until the turn reaches a terminal model
        # completion; ``notified`` on a staged-but-not-yet-modeled turn is the
        # observation ledger, NOT proof of model completion.
        existing = self._records.get(key)
        if existing is not None and existing.get("outcome") in _TERMINAL_OUTCOMES:
            return
        if existing is not None and existing.get("outcome") == _OUTCOME_PENDING:
            # Remove the stale pending record so the retry can re-record; it is
            # not a terminal dedup. (The reason is preserved in the audit log.)
            self._records.pop(key, None)
        # Authoritative pre-check: already notified in the source run → durable discard.
        try:
            if cmd.task_id in self._dispatcher.notified_task_ids(
                cmd.source_run_id, cmd.bundle_digest, cmd.session_id
            ):
                if await asyncio.to_thread(
                    self._record_outcome, cmd, _OUTCOME_DISCARDED, reason="already_notified"
                ):
                    self._pending.pop(key, None)
                self._audit("discarded", cmd, reason="already_notified")
                return
        except Exception as exc:
            logger.debug(
                "background_wakeup.notified_query_error task_id=%s err=%s",
                cmd.task_id, type(exc).__name__,
            )
        # Authoritative pre-check: source canceled → durable discard.
        try:
            if self._dispatcher.is_source_canceled(
                cmd.source_run_id, cmd.bundle_digest, cmd.session_id
            ):
                if await asyncio.to_thread(
                    self._record_outcome, cmd, _OUTCOME_DISCARDED, reason="source_canceled"
                ):
                    self._pending.pop(key, None)
                self._audit("discarded", cmd, reason="source_canceled")
                return
        except Exception as exc:
            logger.debug(
                "background_wakeup.canceled_query_error task_id=%s err=%s",
                cmd.task_id, type(exc).__name__,
            )
        # Transient contention: session has an active turn → retain for retry.
        session_key = (cmd.agent_id, cmd.session_id)
        if (session_key in self._firing_sessions
                or not self._is_idle(cmd.agent_id, cmd.session_id)):
            self._audit("session_not_idle", cmd)
            return
        self._firing.add(key)
        self._firing_sessions.add(session_key)
        try:
            try:
                result = await self._dispatcher.admit(
                    scope_id=cmd.scope_id, source_run_id=cmd.source_run_id,
                    task_id=cmd.task_id, bundle_digest=cmd.bundle_digest,
                    session_id=cmd.session_id,
                    build_id=cmd.build_id, model=cmd.model,
                    approval_mode=cmd.approval_mode,
                )
            except Exception as exc:
                logger.warning(
                    "background_wakeup.admit_error task_id=%s err=%s",
                    cmd.task_id, type(exc).__name__,
                )
                result = "pending"
            if result == "admitted":
                if await asyncio.to_thread(self._record_outcome, cmd, _OUTCOME_ADMITTED):
                    self._pending.pop(key, None)
                self._audit("admitted", cmd)
            elif result == "discarded":
                if await asyncio.to_thread(
                    self._record_outcome, cmd, _OUTCOME_DISCARDED, reason=str(result)
                ):
                    self._pending.pop(key, None)
                self._audit("discarded", cmd, reason=str(result))
            else:
                # Transient (pending): retain for retry. Persist a pending
                # outcome WITH a reason so a cold restart can recover the watch
                # and retry (a pending is not a terminal resolution — unlike
                # admitted/discarded, it does not dedup a cold-start re-register).
                self._defer(cmd)
                await asyncio.to_thread(
                    self._record_outcome, cmd, _OUTCOME_PENDING, reason=str(result)
                )
                self._audit("pending_admission", cmd, reason=str(result))
        finally:
            self._firing.discard(key)
            self._firing_sessions.discard(session_key)

    def _defer(self, cmd: _PendingCommand) -> None:
        # Retain recoverable work indefinitely; a fixed attempt cap could lose a
        # valid notification during a long outage. Back off to at most one
        # admission per minute per watch instead of compiling at poll frequency.
        cmd.retry_attempts += 1
        delay = max(0.1, self._poll_interval) * 2 ** min(cmd.retry_attempts - 1, 10)
        cmd.retry_after = time.time() + min(60.0, delay)

    # -- durable outcome records ----------------------------------------

    def _load_records(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        # A missing records file on first start is normal (no prior outcomes).
        # An existing but corrupt/unreadable/type-invalid file is a hard failure:
        # returning empty would silently reintroduce duplicate admission. Raise so
        # the coordinator does not start with a wiped audit/dedup store.
        path = self._records_path
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as source:
                data = json.load(source)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"background_wakeup: corrupt records file {path}: {type(exc).__name__}"
            ) from exc
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise RuntimeError(
                f"background_wakeup: invalid records structure in {path}"
            )
        result: dict[tuple[str, str, str], dict[str, Any]] = {}
        _REQUIRED = {"scope_id", "source_run_id", "task_id", "outcome"}
        for item in records:
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"background_wakeup: corrupt record entry in {path}"
                )
            missing = _REQUIRED - set(item)
            if missing:
                raise RuntimeError(
                    f"background_wakeup: missing fields {missing} in record in {path}"
                )
            # Validate outcome is a known durable value.
            if item["outcome"] not in _DURABLE_OUTCOMES:
                raise RuntimeError(
                    f"background_wakeup: invalid outcome '{item['outcome']}' in {path}"
                )
            # Validate identity fields are non-empty strings (not None/list/etc).
            for field in ("scope_id", "source_run_id", "task_id"):
                val = item[field]
                if not isinstance(val, str) or not val:
                    raise RuntimeError(
                        f"background_wakeup: invalid {field} in record in {path}"
                    )
            key = (item["scope_id"], item["source_run_id"], item["task_id"])
            if key in result:
                raise RuntimeError(
                    f"background_wakeup: duplicate record entry for {key} in {path}"
                )
            result[key] = item
        return result

    def _record_outcome(self, cmd: _PendingCommand, outcome: str, *, reason: str = "") -> bool:
        with self._record_lock:
            return self._write_outcome(cmd, outcome, reason=reason)

    def _write_outcome(self, cmd: _PendingCommand, outcome: str, *, reason: str = "") -> bool:
        """Persist a durable outcome. Returns True if persisted, False on write failure.

        On write failure the in-memory record is NOT set and the caller must NOT
        pop the pending watch — a failed persist means the outcome is not durable
        and the watch must be retained (not silently dropped or faked as delivered).
        """
        key = cmd.key()
        record = {
            "outcome": outcome, "scope_id": cmd.scope_id,
            "source_run_id": cmd.source_run_id, "session_id": cmd.session_id,
            "task_id": cmd.task_id, "time": time.time(),
            # Full command fields so a non-terminal (pending_admission) record can
            # be rebuilt and retried on cold restart. Terminal outcomes keep them
            # for audit completeness; recovery only re-adds pending records.
            "build_id": cmd.build_id, "agent_id": cmd.agent_id,
            "native_run_id": cmd.native_run_id, "bundle_digest": cmd.bundle_digest,
            "workspace_root": str(cmd.workspace_root),
            "command_state_root": str(cmd.command_state_root),
            "studio_run_id": cmd.studio_run_id,
            "model": cmd.model, "approval_mode": cmd.approval_mode,
            "retry_attempts": cmd.retry_attempts, "retry_after": cmd.retry_after,
            **({"reason": reason} if reason else {}),
        }
        try:
            self._records_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._records_path.with_name(
                f".{self._records_path.name}.{os.getpid()}.{time.time_ns()}"
            )
            # Build the full records list including the new entry, but do NOT
            # commit it to self._records until the file write succeeds.
            pending_records = {**self._records, key: record}
            # Engine notification receipts remain the authority. Old discarded
            # watch audits need not inflate every future write forever.
            terminal = sorted((k for k, r in pending_records.items()
                               if r.get("outcome") == _OUTCOME_DISCARDED),
                              key=lambda k: pending_records[k].get("time", 0), reverse=True)
            for stale in terminal[1000:]:
                pending_records.pop(stale, None)
            with open(temporary, "w", encoding="utf-8") as out:
                json.dump(
                    {"records": list(pending_records.values())}, out, sort_keys=True,
                )
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self._records_path)
        except OSError as exc:
            logger.error(
                "background_wakeup.outcome_persist_failed task_id=%s outcome=%s err=%s",
                cmd.task_id, outcome, type(exc).__name__,
            )
            return False  # not durable — caller retains the watch
        # Persist succeeded: commit to in-memory records.
        self._records = pending_records
        return True

    def _audit(self, outcome: str, cmd: _PendingCommand, *, reason: str = "") -> None:
        logger.info(
            "background_wakeup.%s task_id=%s session=%s build=%s source_run=%s%s",
            outcome, cmd.task_id, cmd.session_id, cmd.build_id, cmd.source_run_id,
            f" reason={reason}" if reason else "",
        )
