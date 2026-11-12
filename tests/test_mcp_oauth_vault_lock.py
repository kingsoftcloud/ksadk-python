"""Vault ownership on the native OS, plus Windows backend selection off Windows."""

import ctypes
import errno
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ksadk.mcp_runtime.oauth.client import BrokerError
from ksadk.mcp_runtime.oauth.local import persistence
from ksadk.mcp_runtime.oauth.local.persistence import LocalVault


def child(directory, body):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path\n"
                "import os, sys\n"
                "from ksadk.mcp_runtime.oauth.local.persistence import LocalVault\n"
                "directory = Path(sys.argv[1])\n" + body
            ),
            str(directory),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_native_cross_process_owner_and_reopen(tmp_path):
    directory = tmp_path / "project"
    vault = LocalVault(directory)
    try:
        vault.data["fixture"] = "saved-value"
        vault.save()
        original_key = (directory / "key").read_bytes()
        result = child(
            directory,
            (
                "try:\n    LocalVault(directory)\n"
                "except ValueError as exc:\n"
                "    assert str(exc) == 'LOCAL_OAUTH_PROJECT_ALREADY_RUNNING'\n"
                "else:\n    raise AssertionError('second owner acquired the vault')\n"
            ),
        )
        assert result.returncode == 0, result.stderr
        assert (directory / "key").read_bytes() == original_key
    finally:
        vault.close()
        vault.close()  # Shutdown cleanup is idempotent.
    result = child(
        directory,
        (
            "vault = LocalVault(directory)\n"
            "assert vault.data['fixture'] == 'saved-value'\n"
            "vault.close()\n"
        ),
    )
    assert result.returncode == 0, result.stderr


def test_native_process_exit_releases_lock(tmp_path):
    result = child(
        tmp_path,
        (
            "vault = LocalVault(directory)\n"
            "vault.data['fixture'] = 'committed-before-exit'\n"
            "vault.save()\n"
            "os._exit(0)\n"  # No Python close/finalizer: the OS must release ownership.
        ),
    )
    assert result.returncode == 0, result.stderr
    vault = LocalVault(tmp_path)
    try:
        assert vault.data["fixture"] == "committed-before-exit"
    finally:
        vault.close()


def test_windows_backend_locks_first_byte_without_importing_fcntl(tmp_path, monkeypatch):
    calls = []

    def locking(fd, mode, length):
        import os

        calls.append((mode, length, os.lseek(fd, 0, os.SEEK_CUR)))

    monkeypatch.setattr(persistence, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=locking))
    monkeypatch.setitem(sys.modules, "fcntl", None)
    path = tmp_path / "owner.lock"
    with path.open("a+b") as stream:
        persistence._lock_project_file(stream)
        assert calls == [(2, 1, 0)]
    assert path.read_bytes() == b""


@pytest.mark.parametrize(
    ("lock_errno", "error", "message"),
    [
        (None, BrokerError, "FILE_LOCK_UNAVAILABLE"),
        (errno.EACCES, ValueError, "PROJECT_ALREADY_RUNNING"),
        (errno.EIO, BrokerError, "FILE_LOCK_UNAVAILABLE"),
    ],
)
def test_failed_windows_acquisition_closes_descriptor(
    tmp_path, monkeypatch, lock_errno, error, message
):
    streams = []
    acquire = persistence._lock_project_file

    def remember(stream):
        streams.append(stream)
        acquire(stream)

    def fail(*args):
        raise OSError(lock_errno, "fixture")

    monkeypatch.setattr(persistence, "sys", SimpleNamespace(platform="win32"))
    backend = SimpleNamespace(LK_NBLCK=2, locking=fail) if lock_errno is not None else None
    monkeypatch.setitem(sys.modules, "msvcrt", backend)
    monkeypatch.setattr(persistence, "_lock_project_file", remember)
    with pytest.raises(error, match=message):
        LocalVault(tmp_path)
    assert len(streams) == 1 and streams[0].closed
    assert not (tmp_path / "key").exists()
    assert not (tmp_path / "vault.sqlite3").exists()


@pytest.mark.parametrize("apply_error", [0, 5])
def test_windows_permissions_apply_protected_dacl_and_free_descriptor(
    tmp_path, monkeypatch, apply_error
):
    from ctypes import wintypes

    def convert(sddl, revision, descriptor, size):
        assert sddl.value == "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)"
        ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = 100
        return True

    def get_acl(descriptor, present, acl, defaulted):
        assert descriptor.value == 100
        ctypes.cast(present, ctypes.POINTER(wintypes.BOOL))[0] = True
        ctypes.cast(acl, ctypes.POINTER(ctypes.c_void_p))[0] = 200
        return True

    advapi = SimpleNamespace(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=convert,
        GetSecurityDescriptorDacl=get_acl,
        SetNamedSecurityInfoW=Mock(return_value=apply_error),
    )
    kernel = SimpleNamespace(LocalFree=Mock())
    monkeypatch.setattr(persistence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **kw: {"advapi32": advapi, "kernel32": kernel}[name],
        raising=False,
    )
    monkeypatch.setattr(ctypes, "WinError", lambda n: OSError(n, "fixture"), raising=False)
    if apply_error:
        with pytest.raises(OSError):
            persistence._private_permissions(tmp_path, 0o700)
    else:
        persistence._private_permissions(tmp_path, 0o700)
    path, kind, flags, owner, group, acl, sacl = advapi.SetNamedSecurityInfoW.call_args.args
    assert (path.value, kind, flags.value, acl.value) == (str(tmp_path), 1, 0x80000004, 200)
    assert owner is group is sacl is None
    assert kernel.LocalFree.call_args.args[0].value == 100
