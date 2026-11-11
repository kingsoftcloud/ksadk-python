"""Agent-owned OAuth MCP declarations; infrastructure is never user input."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)


class OAuthOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopes: list[str] = Field(min_length=1, max_length=32)
    client_id: str = Field(default="", max_length=256)
    token_endpoint_auth_method: Literal["none", "client_secret_basic", "client_secret_post"] = (
        "none"
    )
    client_secret: SecretStr | None = Field(default=None, exclude=False)
    client_ref: str = Field(default="", max_length=64)

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, values):
        if any(not v or len(v) > 256 or any(c.isspace() or ord(c) < 33 for c in v) for v in values):
            raise ValueError("Invalid MCP scope")
        return sorted(set(values))

    @model_validator(mode="after")
    def valid_client(self):
        if self.client_secret is not None and (not self.client_id or self.client_ref):
            raise ValueError("MCP client secret requires client_id and cannot accompany client_ref")
        if self.token_endpoint_auth_method != "none" and not (
            self.client_secret or self.client_ref
        ):
            raise ValueError("Confidential MCP client requires credentials")
        return self


class McpServerDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    url: str = Field(max_length=2048)
    auth_mode: Literal["oauth"]
    oauth: OAuthOptions
    tool_filter: list[str] = Field(default_factory=list, max_length=256)
    tool_policies: dict[str, dict] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value, info: ValidationInfo):
        parts = urlsplit(value)
        loopback = (
            bool((info.context or {}).get("allow_loopback"))
            and parts.scheme == "http"
            and parts.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        if (
            (parts.scheme != "https" and not loopback)
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise ValueError("OAuth MCP requires an HTTPS URL without credentials or fragment")
        return value

    @field_validator("tool_policies")
    @classmethod
    def valid_policies(cls, value):
        import json

        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        if len(json.dumps(value)) > 32768:
            raise ValueError("MCP tool policies are too large")
        for schema in value.values():
            if "$ref" in json.dumps(schema) or "$dynamicRef" in json.dumps(schema):
                raise ValueError("MCP tool policies must be self-contained")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError:
                raise ValueError("MCP tool policy is not a valid JSON Schema") from None
        return value


def normalize_mcp_servers(raw, *, allow_loopback=False):
    if not isinstance(raw, list) or len(raw) > 64:
        raise ValueError("MCPServers must be an array (null is not supported)")
    result = [
        McpServerDeclaration.model_validate(
            item, context={"allow_loopback": allow_loopback}
        ).model_dump(exclude_none=True)
        for item in raw
    ]
    if len({item["name"] for item in result}) != len(result):
        raise ValueError("MCP server names must be unique within an Agent")
    return result
