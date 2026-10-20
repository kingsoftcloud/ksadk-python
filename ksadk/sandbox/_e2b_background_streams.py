"""Remote E2B streams and the deterministic local lifecycle transport.

Transport adapters own SDK and subprocess details; durable record ownership
and supervisor outcome decisions remain in their respective modules.
"""

from __future__ import annotations

import fcntl
import logging
import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from ksadk.sandbox._e2b_background_files import _atomic_json, _json, _open_private
from ksadk.sandbox.base import SandboxError

logger = logging.getLogger("ksadk.sandbox.e2b_background")


class BackgroundStream(Protocol):
    """Supervisor transport: iteration, observed terminal result and disconnect."""

    __module__ = "ksadk.sandbox.e2b_background"

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
