"""Host-owned command records, independent of disposable tool workers.

The supervisor owns the command's existing bounded process group. Cancellation
uses an owned record, never a model-supplied PID. A lost supervisor is an unknown
outcome and is not relaunched. This is lifecycle control, not OS isolation.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ksadk.sandbox.base import SandboxCommandResult, SandboxError
from ksadk.sandbox.local_controls import LocalControlSettings, run_bounded_process

_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "output_limit", "unknown"}
_COMMAND_ID = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class CommandOutput:
    status: str
    stdout: str
    stderr: str
    stdout_cursor: int
    stderr_cursor: int
    exit_code: int | None
    cancel_requested: bool = False
    evidence_incomplete: bool = False
    observed_exit_code: int | None = None
    outcome_reason: str = ""


def _private_directory(path: Path) -> None:
    # The configured root is host-owned, outside the model's editable workspace.
    # Reject symlink components instead of resolving them to another owner's data.
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


class LocalBackgroundCommand:
    def __init__(self, directory: Path, *, owner: str, command_id: str):
        self._directory = directory
        self._owner = owner
        self.command_id = command_id
        self._record()  # Refuse a handle from another session/root.

    def _record(self) -> dict[str, Any]:
        record = _json(self._directory / "record.json")
        if record.get("owner") != self._owner or record.get("command_id") != self.command_id:
            raise SandboxError("Background command does not belong to this session")
        return record

    @property
    def timeout_seconds(self) -> int:
        return self._record()["process_timeout_seconds"]

    @property
    def process_id(self) -> int:
        # Compatibility locator only; no action sends signals to this stored PID.
        process_id = self._record().get("supervisor_pid")
        if type(process_id) is not int or process_id < 1:
            raise SandboxError("Background command supervisor identity is unavailable")
        return process_id

    def read_output(
        self, *, stdout_cursor: int = 0, stderr_cursor: int = 0, max_bytes: int = 65536
    ) -> CommandOutput:
        if type(max_bytes) is not int or not 4 <= max_bytes <= 65536:
            raise SandboxError("Command output window must be between 4 and 65536 bytes")
        import fcntl

        # Observe the record while the ownership check is still valid. If we
        # acquire the lock, no supervisor can start or settle until this read
        # completes. If it is held by the supervisor, a concurrently completed
        # process may conservatively appear running for one more observation.
        # Reading the record *before* checking/releasing the lock could turn a
        # durably settled command into a false unknown and discard its exit code.
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
        )

    def request_cancel(self) -> CommandOutput:
        if self.read_output().status not in _TERMINAL:
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
        # Wait returns the complete bounded result, rather than one output window.
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


def _read_window_all(path: Path) -> tuple[str, int]:
    with os.fdopen(_open_private(path, os.O_RDONLY), "rb") as source:
        data = source.read()
    return data.decode("utf-8"), len(data)


class LocalCommandStore:
    def __init__(
        self, *, state_root: Path, workspace_root: Path, session_id: str, backend_name: str
    ):
        if os.name != "posix":
            raise SandboxError("Background local commands require POSIX process controls")
        root = state_root.expanduser().absolute()
        workspace = workspace_root.resolve()
        if root.resolve() == workspace or workspace in root.resolve().parents:
            raise SandboxError("Background command state must be outside the editable workspace")
        self.owner = hashlib.sha256(
            json.dumps([str(workspace), backend_name, session_id], separators=(",", ":")).encode()
        ).hexdigest()
        self.directory = root / "commands" / self.owner
        _private_directory(self.directory)

    def connect(self, command_id: str) -> LocalBackgroundCommand:
        if not isinstance(command_id, str) or not _COMMAND_ID.fullmatch(command_id):
            raise SandboxError("Invalid background command identity")
        directory = self.directory / command_id
        if directory.is_symlink() or not (directory / "record.json").is_file():
            raise SandboxError("Background command is not owned by this session")
        return LocalBackgroundCommand(directory, owner=self.owner, command_id=command_id)

    def start(
        self,
        *,
        command: str,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        settings: LocalControlSettings,
        operation_key: str | None = None,
    ) -> LocalBackgroundCommand:
        import fcntl

        command_id = hashlib.sha256(
            (self.owner + ":" + (operation_key or uuid4().hex)).encode()
        ).hexdigest()
        digest = hashlib.sha256(
            json.dumps([command, str(cwd), env, timeout, asdict(settings)], sort_keys=True).encode()
        ).hexdigest()
        with os.fdopen(
            _open_private(self.directory / "launch.lock", os.O_RDWR | os.O_CREAT), "r+"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
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
                },
            )
            source_root = str(Path(__file__).resolve().parents[2])
            script = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from ksadk.sandbox.local_background import _supervise; _supervise()"
            )
            handle = self.connect(command_id)
            try:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-I", "-c", script, source_root, str(directory)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                    # The supervisor imports trusted code, without inheriting secrets.
                    env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                )
            except OSError:
                record = handle._record()
                record.update(status="unknown", exit_code=None)
                _atomic_json(directory / "record.json", record)
                return handle
            threading.Thread(target=process.wait, daemon=True).start()
            assert process.stdin is not None
            try:
                process.stdin.write(
                    json.dumps(
                        {
                            "command": command,
                            "cwd": str(cwd),
                            "env": env,
                            "timeout": timeout,
                            "settings": asdict(settings),
                        }
                    ).encode()
                )
                process.stdin.close()
            except OSError:
                # Return the stable locator even when launch acknowledgement is
                # uncertain. The caller must inspect it rather than start again.
                process.stdin.close()
                return handle
            deadline = time.monotonic() + 5
            while handle._record()["status"] == "starting" and time.monotonic() < deadline:
                time.sleep(0.02)
            return handle


def _supervise() -> None:
    import fcntl

    directory = Path(sys.argv[2])
    with os.fdopen(_open_private(directory / "supervisor.lock", os.O_RDWR), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = _json(directory / "record.json")
        cancelled = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: cancelled.set())
        try:
            request = json.load(sys.stdin)
            settings = LocalControlSettings(**request["settings"])
            record.update(status="running", supervisor_pid=os.getpid())
            _atomic_json(directory / "record.json", record)
            output_error: list[Exception] = []

            def on_output(channel: str, text: str) -> None:
                try:
                    with os.fdopen(
                        _open_private(directory / channel, os.O_WRONLY | os.O_APPEND), "ab"
                    ) as out:
                        out.write(text.encode("utf-8"))
                except Exception as exc:
                    output_error.append(exc)
                    cancelled.set()

            executed = run_bounded_process(
                ["/bin/sh", "-c", request["command"]],
                cwd=Path(request["cwd"]),
                env=request["env"],
                timeout=request["timeout"],
                settings=settings,
                start_new_session=True,
                on_output=on_output,
                cancel_requested=lambda: cancelled.is_set() or (directory / "cancel").exists(),
            )
            status = "completed" if executed.exit_code == 0 else "failed"
            exit_code = executed.exit_code
            diagnostic = ""
            if executed.cancelled:
                status = "cancelled"
            elif executed.timed_out:
                status, exit_code, diagnostic = "timed_out", 124, "command timed out"
            elif executed.output_limit_exceeded:
                status, exit_code, diagnostic = "output_limit", 122, "command output limit exceeded"
            if (
                executed.controls.get("status") == "failed"
                or executed.cleanup_error
                or output_error
            ):
                status, exit_code, diagnostic = (
                    "unknown",
                    None,
                    "command supervision or cleanup failed",
                )
            stderr = executed.stderr
            if diagnostic:
                stderr += ("" if not stderr or stderr.endswith("\n") else "\n") + diagnostic
            # The bounded runner drains trailing bytes after process cleanup.
            # Publish those complete results before committing a terminal status.
            for channel, text in (("stdout", executed.stdout), ("stderr", stderr)):
                final_output = directory / f".{channel}.final"
                with os.fdopen(
                    _open_private(final_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
                ) as out:
                    out.write(text.encode("utf-8"))
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(final_output, directory / channel)
            record.update(status=status, exit_code=exit_code, completed_at=time.time())
            _atomic_json(directory / "record.json", record)
        except Exception:
            # Never convert a failed supervisor into a successful command result.
            record.update(status="unknown", exit_code=None, completed_at=time.time())
            _atomic_json(directory / "record.json", record)
