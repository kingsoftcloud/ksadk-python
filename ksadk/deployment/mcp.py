"""Publish OAuth declarations through the existing Agent ComponentConfig."""

from __future__ import annotations

import os

from ksadk.mcp_runtime.oauth.declarations import normalize_mcp_servers


def project_mcp_declarations(config, *, project_dir=None, environment=None):
    """None means omitted; [] explicitly removes OAuth connections.

    Existing static-key/no-auth MCP configuration keeps its runtime path.
    Secrets are read from the developer's environment, never written to YAML.
    """
    if "mcp_servers" not in config:
        return None
    values = {}
    if project_dir is not None:
        from pathlib import Path

        from dotenv import dotenv_values

        values.update(dotenv_values(Path(project_dir) / ".env"))
    values.update(os.environ)
    values.update(environment or {})
    entries = config["mcp_servers"]
    if not isinstance(entries, list):
        raise ValueError("mcp_servers must be an array")
    declarations = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("mcp_servers entries must be objects")
        if entry.get("auth_mode") != "oauth":
            continue
        declaration = dict(entry)
        oauth = dict(declaration.get("oauth") or {})
        if "client_secret" in oauth:
            raise ValueError(
                "Use oauth.client_secret_env instead of a secret in project configuration"
            )
        env_name = oauth.pop("client_secret_env", None)
        if env_name:
            value = values.get(env_name)
            if not value:
                raise ValueError("MCP client secret environment variable is missing")
            oauth["client_secret"] = value
        declaration["oauth"] = oauth
        declarations.append(declaration)
    # Preserve SecretStr until the private request serializer needs its value.
    return normalize_mcp_servers(declarations)


def component_mcp_payload(declarations):
    result = normalize_mcp_servers(declarations)
    for declaration in result:
        secret = declaration["oauth"].get("client_secret")
        if secret is not None:
            declaration["oauth"]["client_secret"] = secret.get_secret_value()
    return result


def redact_mcp_secrets(value):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k == "client_secret" else redact_mcp_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_mcp_secrets(v) for v in value]
    return value


def exclude_mcp_client_secrets(env_vars, declarations):
    """The publisher must not also forward a client secret into runtime env."""
    secrets = []
    for declaration in declarations or []:
        value = (declaration.get("oauth") or {}).get("client_secret")
        if value is not None:
            value = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
            if value:
                secrets.append(value)
    return {
        key: value
        for key, value in env_vars.items()
        if not any(secret in str(value) for secret in secrets)
    }
