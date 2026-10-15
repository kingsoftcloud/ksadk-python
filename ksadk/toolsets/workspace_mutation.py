"""Local file mutation primitives; neither a Run ledger nor a distributed lock.

SDK callers in one process serialize by canonical path. Builtin workers retain
their host's SQLite serialization. Unrelated external writers do not obey these
locks: atomic publication prevents partial files, not arbitrary filesystem CAS.
"""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Lock, RLock
from weakref import WeakValueDictionary

_LOCKS: WeakValueDictionary[str, RLock] = WeakValueDictionary()
_LOCKS_GUARD = Lock()


@contextmanager
def locked_workspace_paths(paths):
    """Acquire a deterministic set of local path locks, including nested calls."""
    keys = sorted({os.path.normcase(str(Path(path).resolve())) for path in paths})
    # Hold strong references before acquiring any lock. Otherwise an unused
    # entry could disappear while another caller prepares the same set.
    with _LOCKS_GUARD:
        locks = []
        for key in keys:
            lock = _LOCKS.get(key)
            if lock is None:
                lock = RLock()
                _LOCKS[key] = lock
            locks.append(lock)
    with ExitStack() as stack:
        for lock in locks:
            stack.enter_context(lock)
        yield


def publish_workspace_bytes(target: Path, payload: bytes, *, create_only: bool = False) -> None:
    """Publish a complete sibling file; never fall back to truncating the target.

    The caller must hold its local mutation lock and validate the observed
    revision. Create-only publication cannot replace a racing new file. A
    failed replace leaves the original content and read-state unchanged.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    try:
        info = target.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("workspace mutation requires a regular file")
        mode = stat.S_IMODE(info.st_mode)
    temporary = target.parent / f".{target.name}.ksadk-{secrets.token_hex(12)}"
    # Preserve ordinary new-file permissions (0666 filtered by the process
    # umask) without reading/changing the process-wide umask in other threads.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            if mode is not None:
                os.chmod(temporary, mode)
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            os.link(temporary, target)
        else:
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
