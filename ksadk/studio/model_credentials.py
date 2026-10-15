"""Workspace and session credential resolution for Studio model transports."""

from __future__ import annotations

import os
from threading import RLock
from typing import Any

from ksadk.studio.errors import StudioError


class CredentialResolver:
    """Resolve Secret references through the shared workspace configuration.

    Workspace credentials use ``.agentkit/config.yaml`` with explicit imports
    and legacy migration. Standalone instances retain process/session behavior.
    """

    _FALLBACK_PAIRS: tuple[tuple[str, str], ...] = (
        ("AGENTKIT_MODEL_API_KEY", "OPENAI_API_KEY"),
        ("OPENAI_API_KEY", "AGENTKIT_MODEL_API_KEY"),
    )

    def __init__(self, workspace: Any = None, *, configuration: Any = None) -> None:
        self._session_values: dict[str, bytearray] = {}
        self._lock = RLock()
        self._workspace = workspace
        from ksadk.studio.configuration import WorkspaceConfiguration

        self.configuration = configuration or (
            WorkspaceConfiguration(workspace) if workspace is not None else None
        )
        self._persisted: dict[str, str] | None = None

    def _load_persisted(self) -> dict[str, str]:
        return self.configuration.secrets() if self.configuration is not None else {}

    @staticmethod
    def _validate_name(name: str) -> str:
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        if not name or any(char not in allowed for char in name):
            raise StudioError(
                "SECRET_REFERENCE_INVALID",
                "环境变量 Secret 引用格式无效",
                status_code=422,
                field="credentialName",
            )
        return name

    @classmethod
    def _environment_name(cls, reference: str) -> str:
        if not reference.startswith("env://"):
            raise StudioError(
                "SECRET_BACKEND_UNAVAILABLE",
                "当前本地 Runtime 仅支持 env:// Secret 引用",
                status_code=501,
                details={"scheme": reference.partition("://")[0]},
            )
        return cls._validate_name(reference.removeprefix("env://"))

    @staticmethod
    def _zero(value: bytearray | None) -> None:
        if value is not None:
            value[:] = b"\x00" * len(value)

    def put_session(self, name: str, value: str) -> dict[str, str | bool]:
        name = self._validate_name(name)
        if (
            not value
            or len(value) > 16_384
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise StudioError(
                "SECRET_VALUE_INVALID",
                "凭证不能为空、包含控制字符或超过 16 KiB",
                status_code=422,
                field="value",
            )
        encoded = bytearray(value.encode("utf-8"))
        with self._lock:
            previous = self._session_values.get(name)
            self._session_values[name] = encoded
            self._zero(previous)
            if self.configuration is not None:
                self.configuration.put_secret(name, value)
        return self.status(f"env://{name}")

    def delete_session(self, name: str) -> dict[str, str | bool]:
        name = self._validate_name(name)
        with self._lock:
            previous = self._session_values.pop(name, None)
            self._zero(previous)
            if self.configuration is not None:
                self.configuration.put_secret(name, None)
        return self.status(f"env://{name}")

    def clear_session(self) -> None:
        with self._lock:
            values = list(self._session_values.values())
            self._session_values.clear()
            for value in values:
                self._zero(value)

    def _fallback_name(self, name: str) -> str | None:
        for primary, alternate in self._FALLBACK_PAIRS:
            if name == primary and alternate != primary:
                return alternate
        return None

    def _resolve_source(self, name: str) -> tuple[bool, str]:
        """Return (configured, source) considering session, persisted file, env, and fallback."""
        fallback = self._fallback_name(name)
        if self.configuration is not None:
            value, source = self.configuration.resolve_candidates(
                [name, *([fallback] if fallback else [])]
            )
            with self._lock:
                # put_session() keeps the session overlay and the persisted copy in
                # sync; if the persisted copy vanished (deleted via another resolver
                # instance) the session entry is stale and must not shadow it.
                if value and name in self._session_values:
                    return True, "session"
                if value and fallback and fallback in self._session_values:
                    return True, "session-alias"
            return bool(value), source
        with self._lock:
            if name in self._session_values:
                return True, "session"
            if fallback and fallback in self._session_values:
                return True, "session-alias"
        persisted = self._load_persisted()
        if name in persisted:
            return True, "workspace"
        if fallback and fallback in persisted:
            return True, "workspace-alias"
        if os.environ.get(name):
            return True, "environment"
        if fallback and os.environ.get(fallback):
            return True, "fallback"
        return False, "missing"

    def _session_value(self, name: str) -> str | None:
        with self._lock:
            value = self._session_values.get(name)
            return value.decode("utf-8") if value is not None else None

    def status(self, reference: str) -> dict[str, str | bool]:
        name = self._environment_name(reference)
        configured, source = self._resolve_source(name)
        return {
            "reference": reference,
            "name": name,
            "configured": configured,
            "source": source,
            "persistence": source,
        }

    def resolve(self, reference: str, *, allow_aliases: bool = True) -> str:
        name = self._environment_name(reference)
        fallback = self._fallback_name(name) if allow_aliases else None
        aliases = [name, *([fallback] if fallback else [])]
        if self.configuration is not None:
            value, _source = self.configuration.resolve_candidates(aliases)
            if value:
                return value
            raise StudioError(
                "SECRET_NOT_FOUND",
                "凭证尚未配置，请在设置中填写",
                status_code=422,
                details={"reference": reference},
            )
        for candidate in aliases:
            value = self._session_value(candidate)
            if value is not None:
                return value
        persisted = self._load_persisted()
        for candidate in aliases:
            if candidate in persisted:
                return persisted[candidate]
        for candidate in aliases:
            value = os.environ.get(candidate)
            if value:
                return value
        raise StudioError(
            "SECRET_NOT_FOUND",
            "模型凭证尚未配置",
            status_code=422,
            details={"reference": reference},
        )

    def exists(self, reference: str) -> bool:
        try:
            return bool(self.status(reference)["configured"])
        except StudioError:
            return False
