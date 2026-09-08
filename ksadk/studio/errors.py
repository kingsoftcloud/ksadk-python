"""Stable errors shared by the Studio service and HTTP API."""

from __future__ import annotations

from typing import Any


class StudioError(Exception):
    """A user-safe error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field
        self.details = details or {}

    def as_dict(self, *, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.field:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        if request_id:
            payload["requestId"] = request_id
        return {"error": payload}


def not_found(resource: str, resource_id: str) -> StudioError:
    return StudioError(
        f"{resource.upper()}_NOT_FOUND",
        f"{resource} 不存在",
        status_code=404,
        details={"id": resource_id},
    )
