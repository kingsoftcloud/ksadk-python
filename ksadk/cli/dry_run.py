"""CLI dry-run utilities."""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional, TypeVar

import click
from click.core import ParameterSource

from ksadk.api.client import DryRunExit
from ksadk.cli.resource_common import build_dry_run_envelope
from ksadk.cli.ui import emit_json, is_json_output

_T = TypeVar("_T")
_DEFAULT_DONE_MSG = "✅ Dry Run Completed: 请求已打印，未执行实际变更。"
_GLOBAL_DRY_RUN_ENV = "AGENTENGINE_GLOBAL_DRY_RUN"


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
        if on_dry_run:
            on_dry_run(exc)
        elif is_json_output() and dry_run_resource and dry_run_action:
            emit_json(
                build_dry_run_envelope(
                    resource=dry_run_resource,
                    action=dry_run_action,
                    request=exc.payload or {},
                    hints=dry_run_hints or [],
                )
            )
        elif exc.payload:
            click.echo("=" * 60)
            click.echo(f"Dry Run Mode: {exc.payload.get('method', 'REQUEST')} {exc.payload.get('url', '')}")
            click.echo("=" * 60)
            click.echo(f"Headers: {exc.payload.get('headers')}")
            if exc.payload.get("body") is not None:
                click.echo(f"Body: {exc.payload.get('body')}")
            if exc.payload.get("curl"):
                click.echo("\nCurl Command:")
                click.echo(str(exc.payload["curl"]))
            click.echo("=" * 60)
        if not is_json_output():
            click.echo(done_message)
        return None
