"""Runtime-owned OAuth declarations; never import or scan an Agent at build time."""

from __future__ import annotations

import copy
from collections.abc import Mapping

from .declarations import normalize_mcp_servers

_CLIENT_FIELDS = {"scopes", "client_id", "token_endpoint_auth_method", "client_secret_env", "client_ref"}
_FIELDS = {"url", "description", "tool_filter", "tool_policies", *_CLIENT_FIELDS}


def _validated(declaration):
    """Validate a secret reference without resolving it during imports/builds."""
    value = copy.deepcopy(declaration)
    oauth = value.get("oauth") or {}
    if "client_secret" in oauth:
        raise ValueError("Use client_secret_env; inline MCP client secrets are not supported")
    env_name = oauth.pop("client_secret_env", None)
    if env_name is not None:
        if not isinstance(env_name, str) or not env_name.isidentifier():
            raise ValueError("Invalid MCP client secret environment reference")
        oauth["client_secret"] = "declaration-validation-placeholder"
    try:
        normalized = normalize_mcp_servers([value], allow_loopback=True)[0]
    except ValueError:
        # Pydantic's default error includes input values. Never echo them.
        raise ValueError(
            "Invalid OAuth MCP connection declaration; check url, scopes and client options"
        ) from None
    normalized["oauth"].pop("client_secret", None)
    if env_name is not None:
        normalized["oauth"]["client_secret_env"] = env_name
    return normalized


def tool_server_declarations(servers):
    """Split model descriptions from immutable, SDK-managed connection data."""
    descriptions, declarations = {}, []
    for name, options in servers.items():
        if isinstance(options, str):
            descriptions[name] = options  # Existing YAML/name-reference API.
            continue
        if not isinstance(options, Mapping) or set(options) - _FIELDS:
            raise ValueError(
                "Invalid McpOAuthTools options; use OAuth fields and secret references"
            )
        options = copy.deepcopy(dict(options))
        description = options.pop("description", "")
        if not isinstance(description, str):
            raise ValueError("MCP description must be a string")
        oauth = {key: options.pop(key) for key in _CLIENT_FIELDS if key in options}
        declaration = {"name": name, "auth_mode": "oauth", "oauth": oauth, **options}
        normalized = _validated(declaration)
        # Keep the existing normalized defaults, so migrating YAML to code
        # preserves the connection/Grant digest for identical configuration.
        declarations.append(normalized)
        descriptions[name] = description
    return descriptions, declarations


def project_declaration(project_dir, name):
    """Resolve only the requested OAuth connection for a managed ZIP or image."""
    import yaml

    from .client import BrokerError
    from .langgraph import bind_declaration, current_declaration

    try:
        with (project_dir / "agentengine.yaml").open(encoding="utf-8-sig") as stream:
            config = yaml.safe_load(stream) or {}
        entries = config.get("mcp_servers", []) if isinstance(config, dict) else None
        if not isinstance(entries, list):
            raise ValueError
        matches = [
            item for item in entries
            if isinstance(item, dict) and item.get("auth_mode") == "oauth" and item.get("name") == name
        ]
        if not matches:
            return None
        if len(matches) != 1 or not isinstance(matches[0].get("oauth"), dict):
            raise ValueError
        with bind_declaration(_validated(matches[0])):
            return current_declaration(name)
    except FileNotFoundError:
        return None
    except (ValueError, TypeError, OSError, yaml.YAMLError):
        # Parser/validation errors may include credentials or source lines.
        raise BrokerError("MCP_CONFIGURATION_INVALID") from None
