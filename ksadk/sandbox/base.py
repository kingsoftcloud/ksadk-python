from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


class SandboxError(RuntimeError):
    pass


class SandboxType(str, Enum):
    AIO = "aio"
    CODE = "code"
    BROWSER = "browser"
    PRIVATE = "private"

    @classmethod
    def from_value(cls, value: str | "SandboxType" | None) -> "SandboxType":
        if isinstance(value, SandboxType):
            return value
        normalized = (
            (value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
        )
        aliases = {
            "": cls.AIO,
            "aio": cls.AIO,
            "allinone": cls.AIO,
            "code": cls.CODE,
            "codeinterpreter": cls.CODE,
            "codesandbox": cls.CODE,
            "browser": cls.BROWSER,
            "browsersandbox": cls.BROWSER,
            "private": cls.PRIVATE,
            "custom": cls.PRIVATE,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise SandboxError(f"Unsupported sandbox type: {value}") from exc


@dataclass(frozen=True)
class SandboxInputFile:
    source: Path
    target_path: str


@dataclass(frozen=True)
class SandboxCommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class SandboxSpec:
    template_id: str
    sandbox_type: SandboxType = SandboxType.AIO
    timeout: int = 900
    allow_internet_access: bool = True
    metadata: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


class SandboxSession(Protocol):
    @property
    def sandbox_id(self) -> str: ...

    def write_file(self, path: str, data: str | bytes) -> None: ...

    def read_file(self, path: str) -> str: ...

    def read_file_bytes(self, path: str, *, max_bytes: int) -> bytes: ...

    def run_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> SandboxCommandResult: ...

    def get_host(self, port: int) -> str: ...

    def kill(self) -> None: ...


@runtime_checkable
class SandboxCommandHandle(Protocol):
    """Optional handle for a command that can be stopped explicitly."""

    def wait(self) -> SandboxCommandResult: ...

    def kill(self) -> bool: ...


@runtime_checkable
class BackgroundCommandSandboxSession(Protocol):
    """Optional session extension used for truthful cooperative cancellation."""

    def start_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> SandboxCommandHandle: ...


@runtime_checkable
class ReconnectableSandboxCommandHandle(SandboxCommandHandle, Protocol):
    """Optional command handle with a credential-free vendor process ID."""

    @property
    def process_id(self) -> int: ...


@runtime_checkable
class ReconnectableCommandSandboxSession(BackgroundCommandSandboxSession, Protocol):
    """Optional session extension for reconnecting an in-flight command."""

    def connect_command(
        self,
        process_id: int,
        *,
        timeout: int | None = None,
    ) -> ReconnectableSandboxCommandHandle: ...


@runtime_checkable
class ArtifactListingSandboxSession(Protocol):
    """Optional session extension for bounded artifact enumeration."""

    def list_files(self, root: str, *, recursive: bool = True) -> list[str]: ...


class SandboxBackend(Protocol):
    def create_session(
        self,
        *,
        session_id: str,
        env: dict[str, str] | None = None,
        input_files: list[SandboxInputFile] | None = None,
    ) -> SandboxSession: ...


@runtime_checkable
class ReconnectableSandboxBackend(Protocol):
    """Optional SDK backend extension for reconnecting an existing session.

    ``session_locator`` is an opaque vendor locator (for example an E2B
    sandbox ID).  It must not contain credentials or environment values.
    """

    def reconnect_session(self, *, session_locator: str) -> SandboxSession: ...
