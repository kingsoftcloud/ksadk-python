"""Native input projection contract consumed by the Codex lifecycle adapter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ksadk.runtime.adapter import ResumePayload, StartRequest


@dataclass(frozen=True)
class ProjectedCodexTurn:
    """SDK-ready turn input; contains no lifecycle or execution behavior."""
    input: Any


class CodexTurnProjection(Protocol):
    def prepare(self, request: StartRequest) -> StartRequest: ...

    def project(
        self, request: StartRequest | None, payload: ResumePayload | None,
    ) -> ProjectedCodexTurn: ...

    def close(self) -> None: ...
