from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass, field
from typing import Any, Coroutine, Sequence, cast
from urllib.parse import urlparse

import httpx
from pydantic import Field

from ksadk.compat.adk_compat import (
    CheckableMcpHttpClientFactory,
    McpToolset,
    StreamableHTTPConnectionParams,
)

MCP_TOOLSET_KEY_ATTR = "_ksadk_mcp_toolset_key"


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    url: str = field(repr=False)
    api_key: str | None = field(default=None, repr=False)
    tool_filter: tuple[str, ...] = ()
    tool_name_prefix: str | None = None

    @property
    def dedupe_key(self) -> str:
        return f"{self.url}|{self.tool_name_prefix or ''}"

    @property
    def headers(self) -> dict[str, str] | None:
        if not self.api_key:
            return None
        return {"Authorization": f"Bearer {self.api_key}"}


def mcp_tools_enabled() -> bool:
    value = os.environ.get("KSADK_ENABLE_MCP_TOOLS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def load_mcp_server_configs(raw_value: str | None = None) -> list[MCPServerConfig]:
    raw = raw_value if raw_value is not None else os.environ.get("KSADK_MCP_SERVERS", "")
    raw = raw.strip()
    if not raw:
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("KSADK_MCP_SERVERS must be a valid JSON array") from exc

    if not isinstance(payload, list):
        raise ValueError("KSADK_MCP_SERVERS must be a JSON array")

    return [_parse_server_config(item, index) for index, item in enumerate(payload)]


def build_connection_params(
    config: MCPServerConfig,
    *,
    httpx_client_factory: CheckableMcpHttpClientFactory | None = None,
) -> StreamableHTTPConnectionParams:
    kwargs: dict[str, Any] = {"url": config.url, "headers": config.headers}
    kwargs["httpx_client_factory"] = httpx_client_factory or _default_httpx_client_factory(
        config.url
    )
    return _RedactedStreamableHTTPConnectionParams(**kwargs)


class _RedactedStreamableHTTPConnectionParams(StreamableHTTPConnectionParams):
    """Keep process-scoped MCP connection material out of debug reprs."""

    url: str = Field(repr=False)
    headers: dict[str, Any] | None = Field(default=None, repr=False)


def build_mcp_toolset(
    config: MCPServerConfig,
    *,
    httpx_client_factory: CheckableMcpHttpClientFactory | None = None,
) -> McpToolset:
    toolset = McpToolset(
        connection_params=build_connection_params(
            config,
            httpx_client_factory=httpx_client_factory,
        ),
        tool_filter=list(config.tool_filter) or None,
        tool_name_prefix=config.tool_name_prefix,
    )
    setattr(toolset, MCP_TOOLSET_KEY_ATTR, config.dedupe_key)
    setattr(toolset, "_ksadk_mcp_server_name", config.name)
    return toolset


def load_mcp_toolsets_from_env() -> list[McpToolset]:
    return [build_mcp_toolset(config) for config in load_mcp_server_configs()]


def register_mcp_toolset_descriptors(toolset: Any) -> list[str]:
    """Best-effort registration of MCP tool descriptors for ksadk tool_search.

    Execution stays with the MCP runtime/toolset. This only makes the tools
    discoverable in ksadk's unified tool catalog.
    """

    try:
        from ksadk.toolsets import register_external_tools
    except Exception:
        return []

    tools = _mcp_toolset_tools_sync(toolset)
    if not tools:
        return []
    server_name = str(getattr(toolset, "_ksadk_mcp_server_name", "") or "mcp").strip() or "mcp"
    return register_external_tools(
        tools,
        group=f"mcp:{server_name}",
        boundary="ksadk_managed_mcp_tool",
        risk_level="medium",
        enabled=True,
    )


def _default_httpx_client_factory(url: str) -> CheckableMcpHttpClientFactory:
    hostname = (urlparse(url).hostname or "").lower()
    trust_env = hostname not in {"localhost", "127.0.0.1", "::1"}

    def _factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=True,
            trust_env=trust_env,
        )

    return _factory


def _mcp_toolset_tools_sync(toolset: Any) -> list[Any]:
    get_tools = getattr(toolset, "get_tools_with_prefix", None)
    if not callable(get_tools):
        return []
    try:
        result = get_tools()
    except Exception:
        return []
    if inspect.isawaitable(result):
        try:
            import asyncio

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(cast(Coroutine[Any, Any, Any], result))
            else:
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                return []
        except Exception:
            return []
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        return list(result)
    return []


def _parse_server_config(item: Any, index: int) -> MCPServerConfig:
    if not isinstance(item, dict):
        raise ValueError(f"KSADK_MCP_SERVERS[{index}] must be an object")

    name = _require_non_empty_string(item.get("name"), f"KSADK_MCP_SERVERS[{index}].name")
    url = _validate_mcp_url(
        _require_non_empty_string(item.get("url"), f"KSADK_MCP_SERVERS[{index}].url")
    )
    api_key = _optional_string(item.get("api_key"))
    tool_filter = tuple(
        _string_list(
            item.get("tool_filter"),
            f"KSADK_MCP_SERVERS[{index}].tool_filter",
        )
    )
    tool_name_prefix = _optional_string(item.get("tool_name_prefix"))

    return MCPServerConfig(
        name=name,
        url=url,
        api_key=api_key,
        tool_filter=tool_filter,
        tool_name_prefix=tool_name_prefix,
    )


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Optional MCP config fields must be strings")
    stripped = value.strip()
    return stripped or None


def _string_list(value: Any, field_name: str) -> Sequence[str]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _validate_mcp_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MCP server url must be an absolute http(s) URL")
    if not parsed.path.endswith("/mcp"):
        raise ValueError("MCP server url must point to a /mcp endpoint")
    return url


__all__ = [
    "MCPServerConfig",
    "MCP_TOOLSET_KEY_ATTR",
    "build_connection_params",
    "build_mcp_toolset",
    "load_mcp_server_configs",
    "load_mcp_toolsets_from_env",
    "mcp_tools_enabled",
    "register_mcp_toolset_descriptors",
]
