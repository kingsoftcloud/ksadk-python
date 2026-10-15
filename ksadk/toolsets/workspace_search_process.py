"""Bounded pipe consumption owned by one foreground ripgrep invocation.

Keep the producer in the caller's process group: the Studio worker supervisor
must still be able to stop it when the outer tool is cancelled or killed.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from queue import Empty, Full, Queue

MAX_RECORD_BYTES = 64 * 1024
_ERROR_BYTES = 8 * 1024
_READ_BYTES = 8 * 1024


class SearchDeadlineExpired(TimeoutError):
    pass


class SearchRecordTooLarge(ValueError):
    pass


class SearchProcess:
    """Drain stderr alongside stdout without collecting an unbounded tail."""

    def __init__(self, command: list[str], *, timeout: float):
        self._deadline = time.monotonic() + timeout
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._chunks: Queue[bytes | OSError | None] = Queue(maxsize=4)
        self._stop = threading.Event()
        self._errors = bytearray()
        self._threads = (
            threading.Thread(target=self._stdout, name="ksadk-workspace-rg-stdout"),
            threading.Thread(target=self._stderr, name="ksadk-workspace-rg-stderr"),
        )

    def __enter__(self):
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        if self._process.poll() is None:
            with suppress(ProcessLookupError):
                self._process.kill()
        self._process.wait()
        for thread in self._threads:
            thread.join()
        self._process.stdout.close()
        self._process.stderr.close()

    def _put(self, chunk: bytes | OSError | None) -> None:
        while not self._stop.is_set():
            try:
                self._chunks.put(chunk, timeout=0.05)
                return
            except Full:
                continue

    def _stdout(self) -> None:
        try:
            while not self._stop.is_set():
                chunk = self._process.stdout.read(_READ_BYTES)
                if not chunk:
                    self._put(None)
                    return
                self._put(chunk)
        except OSError as error:
            self._put(error)

    def _stderr(self) -> None:
        while not self._stop.is_set():
            chunk = self._process.stderr.read(_READ_BYTES)
            if not chunk:
                return
            self._errors.extend(chunk[: max(0, _ERROR_BYTES - len(self._errors))])

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise SearchDeadlineExpired
        return remaining

    def lines(self) -> Iterator[str]:
        buffered = bytearray()
        while True:
            try:
                chunk = self._chunks.get(timeout=self._remaining())
            except Empty:
                raise SearchDeadlineExpired from None
            if chunk is None:
                if buffered:
                    yield buffered.decode("utf-8")
                return
            if isinstance(chunk, OSError):
                raise ValueError("unreadable search pipe") from chunk
            buffered.extend(chunk)
            while (end := buffered.find(b"\n")) >= 0:
                if end > MAX_RECORD_BYTES:
                    raise SearchRecordTooLarge
                record = bytes(buffered[:end])
                del buffered[: end + 1]
                if record:
                    yield record.decode("utf-8")
            if len(buffered) > MAX_RECORD_BYTES:
                raise SearchRecordTooLarge

    def finish(self) -> tuple[int, str]:
        try:
            code = self._process.wait(timeout=self._remaining())
        except subprocess.TimeoutExpired:
            raise SearchDeadlineExpired from None
        self._threads[1].join(timeout=self._remaining())
        if self._threads[1].is_alive():
            raise SearchDeadlineExpired
        return code, self._errors.decode("utf-8", errors="replace")
