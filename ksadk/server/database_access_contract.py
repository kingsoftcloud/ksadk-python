"""CheckAgentDatabaseAccess v1 wire contract (mirrored by Server, no SDK dependency)."""

from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Literal

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

ACTION = "CheckAgentDatabaseAccess"
PATH = f"/agentengine/api/v1/{ACTION}"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectionTarget:
    host: str
    port: int
    user: str = field(repr=False)
    password: str = field(repr=False)
    database: str
    sslmode: str


class DatabaseAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    AgentId: str = Field(min_length=1, max_length=128, strict=True)
    Host: str = Field(min_length=1, max_length=253, strict=True)
    Port: int = Field(default=5432, ge=1, le=65535, strict=True)
    Username: str = Field(min_length=1, max_length=128, strict=True, repr=False)
    Password: SecretStr = Field(
        default_factory=lambda: SecretStr(""), json_schema_extra={"maxLength": 8192}
    )
    DatabaseName: str = Field(min_length=1, max_length=128, strict=True)
    SSLMode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    TimeoutSeconds: int = Field(default=5, ge=1, le=10, strict=True)
    Schema: str | None = Field(default=None, min_length=1, max_length=128, strict=True)
    Tables: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("Host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if ":" in value:
            try:
                ipaddress.IPv6Address(value)
            except ValueError:
                raise ValueError("Host must be one IP address or hostname") from None
            if "%" in value:
                raise ValueError("Scoped IPv6 addresses are not supported")
        elif not all(
            re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?", label)
            for label in value.removesuffix(".").split(".")
        ):
            raise ValueError("Host must be one IP address or hostname")
        return value

    @field_validator("Password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) > 8192 or "\x00" in raw:
            raise ValueError("Invalid password")
        return value

    @field_validator("AgentId", "Schema", "Username", "DatabaseName")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("Invalid identifier")
        return value

    @field_validator("Tables", mode="before")
    @classmethod
    def validate_tables(cls, value):
        if (
            not isinstance(value, list)
            or len(value) > 50
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > 128
                or any(ord(c) < 32 for c in item)
                for item in value
            )
        ):
            raise ValueError("Invalid table names")
        return list(dict.fromkeys(value))

    def wire_payload(self) -> dict:
        return {
            **self.model_dump(exclude={"Password"}, exclude_none=True),
            "Password": self.Password.get_secret_value(),
        }

    def connection_target(self) -> ConnectionTarget:
        """Use explicit fields without building a URI or consulting pod credentials."""
        return ConnectionTarget(
            self.Host,
            self.Port,
            self.Username,
            self.Password.get_secret_value(),
            self.DatabaseName,
            self.SSLMode,
        )


class Ownership(BaseModel):
    Name: str | None
    Owner: str | None
    IsOwner: bool | None
    HasOwnerPrivileges: bool | None


class SchemaAccess(BaseModel):
    Name: str | None
    Exists: bool | None
    Owner: str | None
    CanUse: bool | None
    CanCreate: bool | None


class TableOwnership(Ownership):
    Schema: str | None
    Exists: bool | None


class PermissionWarning(BaseModel):
    Code: str
    Message: str
    Schema: str | None = None
    Table: str | None = None


class Permissions(BaseModel):
    Status: str
    SessionUser: str | None
    CurrentUser: str | None
    IsSuperuser: bool | None
    Database: Ownership
    Schema: SchemaAccess
    Tables: list[TableOwnership]
    Warnings: list[PermissionWarning]


class DatabaseAccessData(BaseModel):
    AgentId: str
    DatabaseType: str
    Success: bool
    Connected: bool
    Stage: str
    ReasonCode: str
    Reason: str
    DurationMs: int
    PodName: str | None
    Permissions: Permissions


class DatabaseAccessResponse(BaseModel):
    Code: int = 0
    Message: str = "Success"
    RequestId: str
    Action: str = ACTION
    Data: DatabaseAccessData


def error_envelope(request_id: str, code: int, reason: str, message: str) -> dict:
    return {
        "Code": code,
        "Message": message,
        "RequestId": request_id,
        "Action": ACTION,
        "ReasonCode": reason,
        "Data": None,
    }


class DatabaseAccessRoute(APIRoute):
    """Scope validation redaction to this Action, including malformed JSON."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            request_id = request.headers.get("X-Ksc-Request-Id") or f"req-{uuid.uuid4().hex[:12]}"
            request.state.database_access_request_id = request_id
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Never log exc, exc.body, input values, arbitrary field names or ctx.
                fields = sorted(
                    {
                        str(err["loc"][1])
                        for err in exc.errors()
                        if len(err.get("loc", ())) > 1
                        and err["loc"][1] in DatabaseAccessRequest.model_fields
                    }
                )
                reason = "INVALID_REQUEST"
                logger.warning(
                    "%s validation_failed request_id=%s fields=%s reason_code=%s",
                    ACTION,
                    request_id,
                    fields,
                    reason,
                )
                return JSONResponse(
                    status_code=422,
                    content=error_envelope(
                        request_id, 422, reason, "请求参数校验失败，请检查数据库连接字段及其他参数"
                    ),
                )

        return safe_handler
