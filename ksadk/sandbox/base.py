from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol


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
        normalized = (value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
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
    def sandbox_id(self) -> str:
        ...

    def write_file(self, path: str, data: str | bytes) -> None:
        ...

    def read_file(self, path: str) -> str:
        ...

    def run_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxCommandResult:
        ...

    def get_host(self, port: int) -> str:
        ...

    def kill(self) -> None:
        ...


class SandboxBackend(Protocol):
    def create_session(
        self,
        *,
        session_id: str,
        env: dict[str, str] | None = None,
        input_files: list[SandboxInputFile] | None = None,
    ) -> SandboxSession:
        ...
