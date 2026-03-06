"""CLI dry-run utilities."""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional, TypeVar

import click

from ksadk.api.client import DryRunExit

_T = TypeVar("_T")
_DEFAULT_DONE_MSG = "✅ Dry Run Completed: 请求已打印，未执行实际变更。"


def is_global_dry_run_enabled() -> bool:
    return os.getenv("AGENTENGINE_GLOBAL_DRY_RUN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def effective_dry_run(local_dry_run: bool = False) -> bool:
    return bool(local_dry_run or is_global_dry_run_enabled())


def dry_run_option(help_text: str = "只打印 curl 请求，不执行"):
    """Reusable Click option for dry-run support."""
    return click.option("--dry-run", is_flag=True, default=False, help=help_text)


def run_async_with_dry_run(
    coro: Awaitable[_T],
    *,
    dry_run: bool,
    done_message: str = _DEFAULT_DONE_MSG,
    on_dry_run: Optional[Callable[[], None]] = None,
) -> Optional[_T]:
    """Run async coroutine and swallow DryRunExit in dry-run mode."""
    _ = dry_run
    try:
        return asyncio.run(coro)
    except DryRunExit:
        if on_dry_run:
            on_dry_run()
        click.echo(done_message)
        return None
