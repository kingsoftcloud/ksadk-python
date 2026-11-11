"""Local-only storage adapters; the hosted Kernel still requires PostgreSQL.

The authorization extension uses a small SQL surface. SQLite supports its
numbered parameters and conflict clauses; only the JSON casts need translation.
Every operation shares the Kernel's lock, connection and fencing transaction.
"""

import errno
import json
import os
import re
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..client import BrokerError
from ..security import canonical


class SQLiteAuthorizationStore:
    def __init__(self, kernel_store):
        from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore

        if not isinstance(kernel_store, SQLiteAgentKernelStore):
            raise ValueError("LOCAL_OAUTH_REQUIRES_SQLITE_KERNEL")
        self.kernel = kernel_store
        self._pool = self

    def __getattr__(self, name):
        return getattr(self.kernel, name)

    @asynccontextmanager
    async def acquire(self):
        async with self.kernel._write_lock:
            connection = await self.kernel._begin()
            try:
                yield _Connection(connection)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _assert_fence(self, connection, instance, session, fence):
        await self.kernel._check_fence(connection.raw, instance, session, fence)


class _Connection:
    def __init__(self, raw):
        self.raw = raw

    @asynccontextmanager
    async def transaction(self):
        # acquire() owns the entire transaction, including the fence check.
        yield

    @staticmethod
    def query(sql, args):
        return re.sub(r"::jsonb\b", "", sql), {str(i + 1): v for i, v in enumerate(args)}

    async def execute(self, sql, *args):
        sql, params = self.query(sql, args)
        for statement in sql.split(";"):
            if statement.strip():
                await self.raw.execute(statement, params)

    async def fetch(self, sql, *args):
        sql, params = self.query(sql, args)
        return await self.raw.execute_fetchall(sql, params)

    async def fetchrow(self, sql, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None


def _lock_project_file(stream):
    """Hold a non-blocking OS lock until the descriptor is closed.

    Windows byte-range locks may extend beyond EOF, so an empty existing lock
    file needs no writes/truncation. Every owner locks the same first byte.
    """
    try:
        if sys.platform == "win32":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        raise BrokerError("LOCAL_OAUTH_FILE_LOCK_UNAVAILABLE") from None
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise ValueError("LOCAL_OAUTH_PROJECT_ALREADY_RUNNING") from None
        raise BrokerError("LOCAL_OAUTH_FILE_LOCK_UNAVAILABLE") from exc


def _private_permissions(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)
        return

    # Windows chmod only toggles the read-only flag. Apply the owner,
    # administrators and SYSTEM ACL to existing vaults as well as new ones.
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    # OI/CI allows files created by SQLite to inherit the directory's DACL.
    flags = "OICI" if mode == 0o700 else ""
    sddl = "D:P" + "".join(f"(A;{flags};FA;;;{sid})" for sid in ("OW", "BA", "SY"))
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        wintypes.LPCWSTR(sddl), 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl = ctypes.c_void_p()
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        if not advapi.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not dacl.value:
            raise BrokerError("LOCAL_OAUTH_VAULT_ACL_UNAVAILABLE")
        protected_dacl = wintypes.DWORD(0x80000004)  # PROTECTED_DACL | DACL_SECURITY_INFORMATION
        error = advapi.SetNamedSecurityInfoW(
            wintypes.LPWSTR(str(path)), 1, protected_dacl, None, None, dacl, None  # SE_FILE_OBJECT
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)


class LocalVault:
    """One OS user/project, one process. Nothing lives in the Agent artifact.

    All OAuth data (including codes, verifier, registration and tokens) is
    encrypted together before SQLite sees it. The local key is protected by
    OS file permissions, not intended as protection against that same OS user.
    """

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private_permissions(directory, 0o700)
        self.lock = open(directory / "owner.lock", "a+b")
        try:
            _lock_project_file(self.lock)
            _private_permissions(directory / "owner.lock", 0o600)
        except BaseException:
            self.lock.close()
            raise
        self.db = None
        try:
            key_path = directory / "key"
            if not key_path.exists():
                if (directory / "vault.sqlite3").exists():
                    raise ValueError("LOCAL_OAUTH_KEY_MISSING")
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(os.urandom(32))
                    stream.flush()
                    os.fsync(stream.fileno())
            _private_permissions(key_path, 0o600)
            self.cipher = AESGCM(key_path.read_bytes())
            self.binding = str(directory.resolve()).encode()
            self.db = sqlite3.connect(directory / "vault.sqlite3")
            _private_permissions(directory / "vault.sqlite3", 0o600)
            self.db.execute("CREATE TABLE IF NOT EXISTS vault (id INTEGER PRIMARY KEY, data BLOB)")
            row = self.db.execute("SELECT data FROM vault WHERE id=1").fetchone()
            self.data = (
                json.loads(self.cipher.decrypt(row[0][:12], row[0][12:], self.binding))
                if row
                else {
                    "waits": {},
                    "grants": {},
                    "configs": {},
                    "clients": {},
                    "transactions": {},
                    "port": 0,
                }
            )
        except BaseException:
            self.close()
            raise

    def save(self):
        nonce = os.urandom(12)
        payload = nonce + self.cipher.encrypt(nonce, canonical(self.data), self.binding)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO vault VALUES(1, ?)", (payload,))

    def close(self):
        try:
            if self.db is not None:
                self.db.close()
        finally:
            self.db = None
            self.lock.close()


def project_vault_directory(project_dir: Path) -> Path:
    return project_dir.resolve() / ".agentengine" / "mcp-oauth"


def open_project_vault(project_dir: Path, *, directory: Path | None = None) -> LocalVault:
    path = project_vault_directory(project_dir)
    if directory is not None and directory != path:
        return LocalVault(directory)
    # Keep local credentials out of Git even in projects not created by CLI init.
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".gitignore").open("a+b") as stream:
        stream.seek(0)
        rules = stream.read()
        if b"/mcp-oauth/" not in rules.removeprefix(b"\xef\xbb\xbf").splitlines():
            newline = b"\r\n" if b"\r\n" in rules else b"\n"
            if rules and not rules.endswith(b"\n"):
                stream.write(newline)
            stream.write(b"/mcp-oauth/" + newline)
    return LocalVault(path)
