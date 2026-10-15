"""Frozen language-server selection supplied by the host, never tool arguments."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from pydantic import Field, JsonValue, model_validator

from ksadk.harness.native.ports import digest
from ksadk.harness.native.state import Contract


class DiagnosticResource(Contract):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def validate_current(self) -> None:
        target = Path(self.path)
        if not target.is_absolute() or not target.is_file():
            raise ValueError("language_server_resource_unavailable")
        with target.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != self.sha256:
            raise ValueError("language_server_resource_changed")


class LanguageServerProfile(Contract):
    server_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    argv: tuple[str, ...] = Field(min_length=1, max_length=32)
    languages: dict[str, str] = Field(min_length=1, max_length=32)
    resources: tuple[DiagnosticResource, ...] = Field(min_length=1, max_length=32)
    initialization: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)

    @model_validator(mode="after")
    def validate_selection(self):
        if not Path(self.argv[0]).is_absolute() or any("\x00" in arg for arg in self.argv):
            raise ValueError("language server requires an absolute executable and literal argv")
        if self.argv[0] not in {resource.path for resource in self.resources}:
            raise ValueError("language server executable must be pinned")
        if any(
            not suffix.startswith(".") or not language
            for suffix, language in self.languages.items()
        ):
            raise ValueError("language server needs explicit suffix-to-language mappings")
        return self

    @property
    def digest(self) -> str:
        return "sha256:" + digest(self)


class DiagnosticDocument(Contract):
    path: str = Field(min_length=1, max_length=512)
    text: str
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_path(self):
        relative = PurePosixPath(self.path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in self.path
            or "\x00" in self.path
        ):
            raise ValueError("diagnostic document must be a workspace-relative path")
        return self
