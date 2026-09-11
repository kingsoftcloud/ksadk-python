"""Stable domain failures; safe to return through the plugin transport."""

from __future__ import annotations


class TeamsError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    def public(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}
