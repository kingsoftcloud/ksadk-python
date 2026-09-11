"""Version-fenced Studio DSH homes; never upgrade an existing session store in place."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ksadk.plugins.dsh_toolchain import DSH_GITHUB_COMMIT, DSH_GITHUB_TAG, DSH_VERSION
from ksadk.plugins.host import PluginHostError

_RECEIPT = ".ksadk-dsh-home.json"
_RECOVERY = (
    "原 DSH 目录已保留。请使用原版本工具链和原目录恢复；"
    "或取消 KSADK_DSH_HOME，使用 Studio 新版本隔离目录，"
    "逐个重装经过验证的固定版本插件。不要把新版 Session 日志交给旧 Core。"
)


class DshHomeVersionError(PluginHostError):
    def __init__(self, diagnostic: dict[str, Any]) -> None:
        self.diagnostic = diagnostic
        super().__init__("dsh_home_version_unverified", _RECOVERY)


def default_studio_dsh_home(workspace: Path) -> Path:
    return workspace.resolve() / ".agentkit" / "dsh-homes" / DSH_VERSION


def studio_dsh_home(workspace: Path) -> Path:
    """Resolve only; safe for read-only catalog routes and lazy construction."""
    configured = os.environ.get("KSADK_DSH_HOME", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else default_studio_dsh_home(workspace)
    )


def _receipt() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "dshVersion": DSH_VERSION,
        "releaseTag": DSH_GITHUB_TAG,
        "sourceCommit": DSH_GITHUB_COMMIT,
        "sessionMigration": "none",
    }


def dsh_home_diagnostic(home: Path, *, workspace: Path | None = None) -> dict[str, Any]:
    """Return bounded compatibility facts without reading logs or exposing paths."""
    result: dict[str, Any] = {
        "expectedVersion": DSH_VERSION,
        "status": "new",
        "writeAllowed": True,
        "reason": "empty_home",
    }
    if workspace is not None:
        legacy = workspace.resolve() / ".agentkit" / "dsh-home"
        result["legacyHomePreserved"] = legacy.is_dir() and legacy.resolve() != home.resolve()
    if not home.exists():
        return result
    if not home.is_dir():
        result.update(status="invalid", writeAllowed=False, reason="home_not_directory")
    else:
        receipt = home / _RECEIPT
        if receipt.is_file() and not receipt.is_symlink():
            try:
                if receipt.stat().st_size > 4096:
                    raise ValueError("oversize receipt")
                payload = json.loads(receipt.read_text())
            except (OSError, ValueError, UnicodeError):
                result.update(status="invalid", writeAllowed=False, reason="receipt_invalid")
            else:
                if payload == _receipt():
                    result.update(status="compatible", reason="receipt_matches")
                    return result
                result.update(status="mismatch", writeAllowed=False, reason="receipt_mismatch")
                version = payload.get("dshVersion") if isinstance(payload, dict) else None
                if isinstance(version, str) and re.fullmatch(
                    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]{1,40})?", version
                ):
                    result["observedVersion"] = version
        elif any(home.iterdir()):
            result.update(status="unverified", writeAllowed=False, reason="receipt_missing")
    if not result["writeAllowed"]:
        result["recovery"] = _RECOVERY
    return result


@contextmanager
def _home_lock(home: Path):
    home.parent.mkdir(parents=True, exist_ok=True)
    lock = home.parent / f".{home.name}.ksadk-version.lock"
    with lock.open("a+b") as stream:
        try:
            import fcntl
        except ImportError:
            import msvcrt

            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def prepare_studio_dsh_home(home: Path) -> None:
    """Before a Core start or Profile mutation, fence a previously clean home."""
    home = home.expanduser().resolve()
    with _home_lock(home):
        diagnostic = dsh_home_diagnostic(home)
        if not diagnostic["writeAllowed"]:
            raise DshHomeVersionError(diagnostic)
        if diagnostic["status"] == "compatible":
            return
        home.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".home-receipt-", dir=home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(_receipt(), stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, home / _RECEIPT)
        finally:
            Path(temporary).unlink(missing_ok=True)
