"""Small process-group lifecycle helpers shared by local runtime backends."""

from __future__ import annotations

import os
import signal
import subprocess
import time


def starts_new_process_group() -> bool:
    return os.name == "posix"


def terminate_process_group(
    process: subprocess.Popen[object],
    *,
    grace_seconds: float = 0.2,
    reap_seconds: float = 1.0,
) -> str | None:
    """Stop a process and its descendants, then reap the direct child.

    The caller must have created the process with ``start_new_session=True`` on
    POSIX. The bounded waits intentionally avoid ``communicate()`` because a
    surviving descendant may keep inherited output descriptors open.
    """

    errors: list[str] = []
    if os.name == "posix":
        _signal_group(process.pid, signal.SIGTERM, errors)
        deadline = time.monotonic() + max(grace_seconds, 0)
        while time.monotonic() < deadline and _group_exists(process.pid):
            time.sleep(0.01)
        if _group_exists(process.pid):
            _signal_group(process.pid, signal.SIGKILL, errors)
    elif process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except Exception as exc:  # pragma: no cover - platform/runtime specific
            errors.append(type(exc).__name__)

    try:
        process.wait(timeout=max(reap_seconds, 0.01))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:  # pragma: no cover - platform/runtime specific
            errors.append(type(exc).__name__)
        try:
            process.wait(timeout=max(reap_seconds, 0.01))
        except subprocess.TimeoutExpired:
            errors.append("process_reap_timeout")
    except Exception as exc:  # pragma: no cover - platform/runtime specific
        errors.append(type(exc).__name__)
    return ",".join(dict.fromkeys(errors)) or None


def _signal_group(pid: int, sig: signal.Signals, errors: list[str]) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except Exception as exc:  # pragma: no cover - platform/runtime specific
        errors.append(type(exc).__name__)


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - cannot inspect another owner
        return True
