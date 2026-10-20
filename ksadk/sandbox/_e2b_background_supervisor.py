"""Detached E2B command supervision and recovery evidence.

Host-owned durable background commands for the E2B sandbox backend.

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

import fcntl
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from ksadk.sandbox._e2b_background_files import (
    _append_output,
    _atomic_json,
    _finalize_output_from_log,
    _json,
    _open_private,
)
from ksadk.sandbox._e2b_background_streams import (
    BackgroundStream,
    _disconnect_stream,
    _safe_kill,
    _StreamAdapter,
)

logger = logging.getLogger("ksadk.sandbox.e2b_background")
_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "output_limit", "unknown"}


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
