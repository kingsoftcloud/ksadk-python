"""Audience-specific projected workload-token loading and validation."""

from __future__ import annotations

import base64
import binascii
import errno
import json
import os
import stat
import time
from abc import ABC, abstractmethod
from pathlib import Path

ENV_A2A_TOKEN_DIR = "KSADK_A2A_TOKEN_DIR"

DEFAULT_A2A_TOKEN_DIR = "/var/run/secrets/agentengine/a2a"

MAX_WORKLOAD_TOKEN_BYTES = 16 * 1024


class WorkloadTokenProvider(ABC):
    """Returns a short-lived workload token for one exact audience."""

    @abstractmethod
    def get_token(self, audience: str) -> str:
        raise NotImplementedError


class FileWorkloadTokenProvider(WorkloadTokenProvider):
    """Reads audience-specific projected JWT files before every request."""

    def __init__(self, token_dir: str | os.PathLike[str] | None = None) -> None:
        configured = token_dir or os.getenv(ENV_A2A_TOKEN_DIR) or DEFAULT_A2A_TOKEN_DIR
        self._token_dir = Path(configured)

    def get_token(self, audience: str) -> str:
        if not audience or "/" in audience or audience in {".", ".."}:
            raise ValueError(f"invalid workload token audience: {audience!r}")
        path = self._token_dir / f"{audience}.jwt"
        if path.is_symlink():
            # AtomicWriter rotates ..data atomically. Only accept its exact key
            # layout, and open the resolved generation without following links.
            if os.readlink(path) != f"..data/{path.name}":
                raise RuntimeError(f"invalid workload token symlink for {audience!r}")
            try:
                volume = self._token_dir.resolve(strict=True)
                relative = path.resolve(strict=True).relative_to(volume)
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                directory = os.open(volume, flags | os.O_DIRECTORY)
                try:
                    for component in relative.parts[:-1]:
                        child = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
                        os.close(directory)
                        directory = child
                    descriptor = os.open(relative.name, flags | os.O_NONBLOCK, dir_fd=directory)
                finally:
                    os.close(directory)
            except (OSError, ValueError, RuntimeError) as exc:
                raise RuntimeError(f"unsafe workload token projection for {audience!r}") from exc
        else:
            descriptor = self._open_regular_token(path, audience)
        return self._read_token(descriptor, path, audience)

    @staticmethod
    def _open_regular_token(path: Path, audience: str) -> int:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(path, flags | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeError(
                    f"workload token for audience {audience!r} must not be a symlink: {path}"
                ) from exc
            raise RuntimeError(f"missing workload token for audience {audience!r}: {path}") from exc

    def _read_token(self, descriptor: int, path: Path, audience: str) -> str:
        try:
            token_stat = os.fstat(descriptor)
            if not stat.S_ISREG(token_stat.st_mode):
                raise RuntimeError(
                    f"workload token for audience {audience!r} is not a regular file: {path}"
                )
            if token_stat.st_size > MAX_WORKLOAD_TOKEN_BYTES:
                raise RuntimeError(
                    f"workload token for audience {audience!r} exceeds 16 KiB: {path}"
                )
            # Kubernetes adds group-read for a configured pod fsGroup. Keep
            # credentials read-only and never permit access by other users.
            if stat.S_IMODE(token_stat.st_mode) not in {0o400, 0o440}:
                raise RuntimeError(
                    f"workload token for audience {audience!r} must have mode 0400 or 0440: {path}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as token_file:
                raw_token = token_file.read(MAX_WORKLOAD_TOKEN_BYTES + 1)
            if len(raw_token) > MAX_WORKLOAD_TOKEN_BYTES:
                raise RuntimeError(
                    f"workload token for audience {audience!r} exceeds 16 KiB: {path}"
                )
            token = raw_token.decode("ascii").strip()
        except UnicodeError as exc:
            raise RuntimeError(
                f"workload token for audience {audience!r} must be ASCII: {path}"
            ) from exc
        finally:
            os.close(descriptor)
        if not token:
            raise RuntimeError(f"empty workload token for audience {audience!r}: {path}")
        self._validate_expiry(token, audience=audience, path=path)
        return token

    @staticmethod
    def _validate_expiry(token: str, *, audience: str, path: Path) -> None:
        parts = token.split(".")
        if len(parts) != 3:
            raise RuntimeError(f"workload token for audience {audience!r} is not a JWT: {path}")
        try:
            payload_segment = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_segment).decode("utf-8"))
            expires_at = int(payload["exp"])
        except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"workload token for audience {audience!r} has no valid exp claim: {path}"
            ) from exc
        token_audience = payload.get("aud")
        accepted_audiences = (
            {str(item) for item in token_audience}
            if isinstance(token_audience, list)
            else {str(token_audience)}
        )
        if audience not in accepted_audiences:
            raise RuntimeError(f"workload token audience mismatch for {audience!r}: {path}")
        if expires_at <= int(time.time()):
            raise RuntimeError(f"workload token for audience {audience!r} is expired: {path}")
