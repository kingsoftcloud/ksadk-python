"""Framework-level common CLI options."""

from __future__ import annotations

import click

from ksadk.cli.dry_run import build_dry_run_click_option
from ksadk.cli.ui import build_no_color_click_option, build_output_click_option


def _has_long_option(command: click.Command, option_name: str) -> bool:
    return any(
        isinstance(param, click.Option) and option_name in param.opts
        for param in command.params
    )


def ensure_global_cli_options(command: click.Command) -> click.Command:
    """Inject hidden common options into groups/commands that do not declare them locally."""
    if not _has_long_option(command, "--dry-run"):
        command.params.insert(0, build_dry_run_click_option(hidden=True, expose_value=False))
    if not _has_long_option(command, "--output"):
        command.params.insert(0, build_output_click_option(hidden=True, expose_value=False))
    if not _has_long_option(command, "--no-color"):
        command.params.insert(0, build_no_color_click_option(hidden=True, expose_value=False))

    if isinstance(command, click.Group):
        for subcommand in command.commands.values():
            ensure_global_cli_options(subcommand)

    return command
