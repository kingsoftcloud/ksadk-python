"""CLI dry-run utilities."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Optional, TypeVar

import click
from click.core import ParameterSource

from ksadk.api.client import DryRunExit
from ksadk.cli.resource_common import build_dry_run_envelope
from ksadk.cli.ui import emit_json, is_json_output

_T = TypeVar("_T")
_DEFAULT_DONE_MSG = "✅ Dry Run Completed: 请求已打印，未执行实际变更。"
_GLOBAL_DRY_RUN_ENV = "AGENTENGINE_GLOBAL_DRY_RUN"
_MASKED_VALUE = "***"
_SENSITIVE_KEY_TOKENS = (
    "authorization",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "signature",
    "accesskey",
    "access_key",
)


def is_global_dry_run_enabled() -> bool:
    return os.getenv(_GLOBAL_DRY_RUN_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def effective_dry_run(local_dry_run: bool = False) -> bool:
    return bool(local_dry_run or is_global_dry_run_enabled())


def _set_dry_run_callback(ctx: click.Context, _param: click.Parameter, value: bool):
    ctx.ensure_object(dict)
    inherited = False
    if ctx.parent is not None and isinstance(ctx.parent.obj, dict):
        inherited = bool(ctx.parent.obj.get("dry_run"))

    source = ctx.get_parameter_source("dry_run")
    selected = inherited if source == ParameterSource.DEFAULT else bool(value)

    if selected:
        os.environ[_GLOBAL_DRY_RUN_ENV] = "1"
    else:
        os.environ.pop(_GLOBAL_DRY_RUN_ENV, None)

    ctx.obj["dry_run"] = selected
    return selected


def dry_run_option(
    help_text: str = "只打印 curl 请求，不执行",
    *,
    hidden: bool = False,
    expose_value: bool = True,
):
    """Reusable Click option for dry-run support."""
    return click.option(
        "--dry-run",
        "dry_run",
        is_flag=True,
        default=False,
        hidden=hidden,
        expose_value=expose_value,
        callback=_set_dry_run_callback,
        help=help_text,
    )


def build_dry_run_click_option(
    help_text: str = "只打印 curl 请求，不执行",
    *,
    hidden: bool = False,
    expose_value: bool = True,
) -> click.Option:
    """Build a dry-run option for command injection."""
    return click.Option(
        ["--dry-run", "dry_run"],
        is_flag=True,
        default=False,
        hidden=hidden,
        expose_value=expose_value,
        callback=_set_dry_run_callback,
        help=help_text,
    )


def _is_sensitive_key(key: Any) -> bool:
    text = str(key or "").strip().lower().replace("-", "_")
    if not text:
        return False
    return any(token in text for token in _SENSITIVE_KEY_TOKENS)


def _mask_if_sensitive(value: Any, *, sensitive: bool) -> Any:
    if not sensitive:
        return value
    if value is None:
        return None
    return _MASKED_VALUE


def _sanitize_dry_run_value(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        env_key = str(value.get("Key") or "")
        env_sensitive = bool(value.get("IsSensitive")) or _is_sensitive_key(env_key)
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            item_sensitive = env_sensitive and key_text == "Value"
            if not item_sensitive and _is_sensitive_key(key_text):
                item_sensitive = True
            sanitized[key_text] = _mask_if_sensitive(
                _sanitize_dry_run_value(item, parent_key=key_text),
                sensitive=item_sensitive,
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_dry_run_value(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_dry_run_value(item, parent_key=parent_key) for item in value)
    return _mask_if_sensitive(value, sensitive=_is_sensitive_key(parent_key))


def _shell_quote_single(value: str) -> str:
    return value.replace("'", "'\"'\"'")


def _build_redacted_curl(method: Any, url: Any, headers: Any, body: Any) -> str:
    method_text = str(method or "REQUEST")
    url_text = str(url or "")
    lines = [f'curl -X {method_text} "{url_text}" \\']
    if isinstance(headers, dict):
        for key, value in headers.items():
            lines.append(f'  -H "{key}: {value}" \\')
    if body is not None:
        body_text = json.dumps(body, ensure_ascii=False)
        lines.append(f"  -d '{_shell_quote_single(body_text)}'")
    elif lines:
        lines[-1] = lines[-1].rstrip(" \\")
    return "\n".join(lines)


def sanitize_dry_run_request(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Redact secrets from dry-run request payloads before printing or JSON output."""
    if not payload:
        return {}
    safe_payload = _sanitize_dry_run_value(dict(payload or {}))
    if not isinstance(safe_payload, dict):
        return {}
    safe_payload["curl"] = _build_redacted_curl(
        safe_payload.get("method"),
        safe_payload.get("url"),
        safe_payload.get("headers"),
        safe_payload.get("body"),
    )
    return safe_payload


def run_async_with_dry_run(
    coro: Awaitable[_T],
    *,
    dry_run: bool,
    done_message: str = _DEFAULT_DONE_MSG,
    on_dry_run: Optional[Callable[[DryRunExit], None]] = None,
    dry_run_resource: str | None = None,
    dry_run_action: str | None = None,
    dry_run_hints: Optional[list[str]] = None,
) -> Optional[_T]:
    """Run async coroutine and swallow DryRunExit in dry-run mode."""
    _ = dry_run
    try:
        return asyncio.run(coro)
    except DryRunExit as exc:
        safe_payload = sanitize_dry_run_request(exc.payload or {})
        if on_dry_run:
            on_dry_run(DryRunExit(str(exc), payload=safe_payload))
        elif is_json_output() and dry_run_resource and dry_run_action:
            emit_json(
                build_dry_run_envelope(
                    resource=dry_run_resource,
                    action=dry_run_action,
                    request=safe_payload,
                    hints=dry_run_hints or [],
                )
            )
        elif safe_payload:
            click.echo("=" * 60)
            click.echo(f"Dry Run Mode: {safe_payload.get('method', 'REQUEST')} {safe_payload.get('url', '')}")
            click.echo("=" * 60)
            click.echo(f"Headers: {safe_payload.get('headers')}")
            if safe_payload.get("body") is not None:
                click.echo(f"Body: {safe_payload.get('body')}")
            if safe_payload.get("curl"):
                click.echo("\nCurl Command:")
                click.echo(str(safe_payload["curl"]))
            click.echo("=" * 60)
        if not is_json_output():
            click.echo(done_message)
        return None
