"""Connector-local HTTP destinations and their public service declarations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from string import Formatter
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

# The transport, response streams and MCP client must use the same backend,
# even when both httpx packages are installed in a legacy MCP environment.
from ksadk._mcp_compat import httpx_module

httpx = httpx_module()
from mcp.types import Tool

from .protocol import TunnelError, identifier

_REQUEST_HEADERS = frozenset(
    {"accept", "content-type", "mcp-session-id", "mcp-protocol-version", "last-event-id"}
)
_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
        "cache-control",
        "retry-after",
        "content-language",
    }
)


def _endpoint(url: str) -> None:
    if not isinstance(url, str):
        raise ValueError("local endpoint must be an absolute http(s) URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "local endpoint must be an absolute http(s) URL without userinfo or fragment"
        )
    try:
        httpx.URL(url)
    except Exception:
        raise ValueError("invalid local endpoint URL") from None


def _names(value, field_name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, (tuple, list))
        or any(not isinstance(name, str) or not name for name in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{field_name} must contain unique non-empty names")
    return tuple(value)


def _schema(schema: dict) -> None:
    from jsonschema import Draft202012Validator

    try:
        if not isinstance(schema, dict):
            raise ValueError
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ValueError("input_schema must be a valid JSON Schema object") from None


def validate_arguments(schema: dict, arguments: dict) -> None:
    """Validate locally; external schema references never perform network I/O."""
    from jsonschema import Draft202012Validator
    from referencing import Registry

    try:
        valid = Draft202012Validator(schema, registry=Registry()).is_valid(arguments)
    except Exception:
        raise TunnelError("local_service_error") from None
    if not valid:
        raise TunnelError("invalid_request")


def _headers(headers: dict[str, str]) -> dict[str, str]:
    try:
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError
        return dict(httpx.Headers(headers))
    except Exception:
        raise ValueError("headers must map header names to string values") from None


def response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Forward protocol metadata, excluding cookies, auth, redirects and encoding.

    The connector decodes HTTP content encoding before emitting UTF-8 chunks,
    so Content-Encoding and Content-Length must not cross the transport.
    """
    return {name: value for name, value in headers.items() if name.lower() in _RESPONSE_HEADERS}


@dataclass(frozen=True)
class LocalMCPService:
    """One fixed Streamable HTTP endpoint; local credentials never register."""

    name: str
    url: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    tool_filter: tuple[str, ...] | None = None

    def __post_init__(self):
        identifier(self.name, "service name")
        _endpoint(self.url)
        object.__setattr__(self, "headers", _headers(self.headers))
        if self.tool_filter is not None:
            object.__setattr__(self, "tool_filter", _names(self.tool_filter, "tool_filter"))

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "protocol": "mcp",
            "tool_filter": list(self.tool_filter) if self.tool_filter is not None else None,
        }


@dataclass(frozen=True)
class APIOperation:
    """Expose a named HTTP operation as a model-bindable tool.

    Path fields are inferred from {name} placeholders when path_params is
    omitted. query_params explicitly selects query arguments. body_params=None
    sends remaining arguments as JSON for POST/PUT/PATCH/DELETE; an empty tuple
    sends no arguments in the body. GET arguments must all be mapped to the
    path or query. No caller argument can change the destination or method.
    """

    name: str
    method: str
    path: str = field(repr=False)
    input_schema: dict
    description: str = ""
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    body_params: tuple[str, ...] | None = None

    def __post_init__(self):
        identifier(self.name, "operation name")
        if not isinstance(self.method, str) or self.method.upper() not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            raise ValueError("operation method must be GET, POST, PUT, PATCH or DELETE")
        object.__setattr__(self, "method", self.method.upper())
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or self.path.startswith("//")
            or any(char in self.path for char in "\\%?#\r\n\x00")
            or any(part in {".", ".."} for part in self.path.split("/"))
        ):
            raise ValueError(
                "operation path must be a fixed absolute path without traversal or query"
            )
        try:
            fields = []
            for _, name, spec, conversion in Formatter().parse(self.path):
                if name is None:
                    continue
                identifier(name, "path parameter")
                if spec or conversion or name in fields:
                    raise ValueError
                fields.append(name)
        except ValueError:
            raise ValueError("operation path must use unique plain {name} parameters") from None
        path_params = _names(self.path_params, "path_params") or tuple(fields)
        if set(path_params) != set(fields):
            raise ValueError("path_params must match operation path placeholders")
        query_params = _names(self.query_params, "query_params")
        body_params = None if self.body_params is None else _names(self.body_params, "body_params")
        mapped = [*path_params, *query_params, *(body_params or ())]
        if len(mapped) != len(set(mapped)):
            raise ValueError("operation argument mappings must not overlap")
        if self.method == "GET" and body_params:
            raise ValueError("GET operations cannot declare body_params")
        if not isinstance(self.description, str):
            raise ValueError("operation description must be a string")
        _schema(self.input_schema)
        object.__setattr__(self, "path_params", path_params)
        object.__setattr__(self, "query_params", query_params)
        object.__setattr__(self, "body_params", body_params)

    def tool(self) -> dict:
        return Tool(
            name=self.name, description=self.description, inputSchema=self.input_schema
        ).model_dump(by_alias=True, mode="json", exclude_none=True)


@dataclass(frozen=True)
class LocalAPIService:
    """A local HTTP API with an explicit, finite operation allowlist."""

    name: str
    base_url: str = field(repr=False)
    operations: tuple[APIOperation, ...]
    headers: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        identifier(self.name, "service name")
        _endpoint(self.base_url)
        if (
            not isinstance(self.operations, (list, tuple))
            or not self.operations
            or any(not isinstance(op, APIOperation) for op in self.operations)
            or len({op.name for op in self.operations}) != len(self.operations)
        ):
            raise ValueError("operations must contain uniquely named APIOperation declarations")
        fixed_query = set(httpx.QueryParams(urlsplit(self.base_url).query))
        if any(set(op.query_params) & fixed_query for op in self.operations):
            raise ValueError(
                "operation query parameters must not override configured URL parameters"
            )
        object.__setattr__(self, "operations", tuple(self.operations))
        object.__setattr__(self, "headers", _headers(self.headers))

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "protocol": "api",
            "tools": [op.tool() for op in self.operations],
        }


def _empty_query(query: Any) -> bool:
    return query in ("", None) or query == {} or query == []


def _json_arguments(body: str) -> dict:
    try:

        def reject_constant(_):
            raise ValueError

        arguments = json.loads(body, parse_constant=reject_constant)
        if not isinstance(arguments, dict):
            raise ValueError
        return arguments
    except (ValueError, TypeError, RecursionError):
        raise TunnelError("invalid_request") from None


def _prepare_mcp_request(service, method, path, forwarded, body) -> httpx.Request:
    if path not in {"", "/"} or method not in {"GET", "POST", "DELETE"}:
        raise TunnelError("forbidden")
    headers = httpx.Headers(
        {k: v for k, v in forwarded.items() if isinstance(k, str) and k.lower() in _REQUEST_HEADERS}
    )
    headers.update(service.headers)
    if method == "POST" and service.tool_filter is not None:
        message = _json_arguments(body)
        if message.get("method") == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or params.get("name") not in service.tool_filter:
                raise TunnelError("forbidden")
    return httpx.Request(method, service.url, headers=headers, content=body.encode("utf-8"))


def _api_target(service, operation, arguments) -> httpx.URL:
    path_values = {}
    for name in operation.path_params:
        value = arguments.get(name)
        if (
            not isinstance(value, (str, int, float))
            or isinstance(value, bool)
            or str(value) in {".", ".."}
            or any(char in str(value) for char in "/\\%\r\n\x00")
        ):
            raise TunnelError("invalid_request")
        path_values[name] = quote(str(value), safe="")
    local = urlsplit(service.base_url)
    target = httpx.URL(
        urlunsplit(
            (
                local.scheme,
                local.netloc,
                local.path.rstrip("/") + operation.path.format_map(path_values),
                local.query,
                "",
            )
        )
    )
    query_values = []
    for name in operation.query_params:
        if name not in arguments:
            continue
        values = arguments[name] if isinstance(arguments[name], list) else [arguments[name]]
        for value in values:
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise TunnelError("invalid_request")
            text = (
                ""
                if value is None
                else str(value).lower() if isinstance(value, bool) else str(value)
            )
            query_values.append((name, text))
    return target.copy_merge_params(query_values) if query_values else target


def _prepare_api_request(service, method, path, body) -> httpx.Request:
    if method != "POST":
        raise TunnelError("forbidden")
    operation = next((op for op in service.operations if op.name == path), None)
    if operation is None:
        raise TunnelError("forbidden")
    arguments = _json_arguments(body)
    validate_arguments(operation.input_schema, arguments)
    target = _api_target(service, operation, arguments)
    remaining = set(arguments) - set(operation.path_params) - set(operation.query_params)
    body_names = (
        remaining
        if operation.body_params is None
        else set(operation.body_params) & arguments.keys()
    )
    if remaining - body_names or (operation.method == "GET" and body_names):
        raise TunnelError("invalid_request")
    headers = httpx.Headers(service.headers)
    if body_names or operation.method in {"POST", "PUT", "PATCH"}:
        payload = {name: arguments[name] for name in body_names}
        return httpx.Request(operation.method, target, headers=headers, json=payload)
    return httpx.Request(operation.method, target, headers=headers)


def prepare_request(service: LocalMCPService | LocalAPIService, frame: dict) -> httpx.Request:
    """Resolve the synthetic request using only connector-owned destinations."""
    try:
        method, path, query, forwarded, body = (
            frame[key] for key in ("method", "path", "query", "headers", "body")
        )
        if (
            not isinstance(method, str)
            or not isinstance(path, str)
            or not isinstance(body, str)
            or not isinstance(forwarded, dict)
            or not _empty_query(query)
        ):
            raise TunnelError("invalid_request")
        if isinstance(service, LocalMCPService):
            return _prepare_mcp_request(service, method, path, forwarded, body)
        if isinstance(service, LocalAPIService):
            return _prepare_api_request(service, method, path, body)
        raise TunnelError("forbidden")
    except TunnelError:
        raise
    except Exception:
        # Never expose URLs, configured headers, schema values or HTTP errors.
        raise TunnelError("invalid_request") from None
