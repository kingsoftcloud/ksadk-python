"""CLI 公共 UI 组件与运行时输出控制。"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
import os
import sys
from typing import Iterator

import click
from click.core import ParameterSource
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.theme import Theme

OUTPUT_MODE_PRETTY = "pretty"
OUTPUT_MODE_JSON = "json"
_OUTPUT_MODE_ENV = "AGENTENGINE_OUTPUT_MODE"
_NO_COLOR_ENV = "AGENTENGINE_NO_COLOR"

STATUS_RICH_STYLE = {
    "RUNNING": "bold #2da44e",
    "READY": "bold #2da44e",
    "HEALTHY": "bold #2da44e",
    "CREATING": "bold #d29922",
    "SUBMITTED": "bold #d29922",
    "PENDING": "bold #d29922",
    "UPDATING": "bold #d29922",
    "SCALING": "bold #d29922",
    "FAILED": "bold #f85149",
    "ERROR": "bold #f85149",
    "TERMINATED": "bold #f85149",
    "UNKNOWN": "bold #c9d1d9",
}

STATUS_CLICK_COLOR = {
    "RUNNING": "green",
    "READY": "green",
    "HEALTHY": "green",
    "CREATING": "yellow",
    "SUBMITTED": "yellow",
    "PENDING": "yellow",
    "UPDATING": "yellow",
    "SCALING": "yellow",
    "FAILED": "red",
    "ERROR": "red",
    "TERMINATED": "red",
    "UNKNOWN": "white",
}

_THEME = Theme(
    {
        "title": "bold #1f6feb",
        "muted": "#8b949e",
        "ok": "bold #2da44e",
        "warn": "bold #d29922",
        "err": "bold #f85149",
    }
)


def _is_truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class UIRuntime:
    output_mode: str = OUTPUT_MODE_PRETTY
    no_color: bool = False
    stdout_is_tty: bool = False


@dataclass
class CapturedOutput:
    stdout: str = ""
    stderr: str = ""

    @property
    def combined(self) -> str:
        return "".join(part for part in (self.stdout, self.stderr) if part)


_RUNTIME = UIRuntime()
_CONSOLE: Console | None = None


def _resolve_output_mode(explicit: str | None = None) -> str:
    mode = explicit or os.getenv(_OUTPUT_MODE_ENV) or OUTPUT_MODE_PRETTY
    normalized = str(mode).strip().lower()
    if normalized not in {OUTPUT_MODE_PRETTY, OUTPUT_MODE_JSON}:
        return OUTPUT_MODE_PRETTY
    return normalized


def _resolve_no_color(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return _is_truthy(os.getenv(_NO_COLOR_ENV)) or _is_truthy(os.getenv("NO_COLOR"))


def _resolve_stdout_is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _build_console() -> Console:
    rich_enabled = (
        _RUNTIME.output_mode == OUTPUT_MODE_PRETTY
        and _RUNTIME.stdout_is_tty
        and not _RUNTIME.no_color
    )
    return Console(
        theme=_THEME,
        no_color=_RUNTIME.no_color or _RUNTIME.output_mode == OUTPUT_MODE_JSON,
        force_terminal=rich_enabled,
        color_system="truecolor" if rich_enabled else None,
        highlight=False,
        soft_wrap=True,
    )


def configure_ui_runtime(
    *,
    output_mode: str | None = None,
    no_color: bool | None = None,
    stdout_is_tty: bool | None = None,
) -> None:
    """Configure per-process CLI UI runtime."""
    global _CONSOLE

    resolved_output = _resolve_output_mode(output_mode)
    resolved_no_color = _resolve_no_color(no_color)
    resolved_tty = _resolve_stdout_is_tty() if stdout_is_tty is None else bool(stdout_is_tty)

    _RUNTIME.output_mode = resolved_output
    _RUNTIME.no_color = resolved_no_color
    _RUNTIME.stdout_is_tty = resolved_tty

    os.environ[_OUTPUT_MODE_ENV] = resolved_output
    if resolved_no_color:
        os.environ[_NO_COLOR_ENV] = "1"
        os.environ.setdefault("NO_COLOR", "1")
    else:
        os.environ.pop(_NO_COLOR_ENV, None)

    _CONSOLE = _build_console()


class ConsoleProxy:
    """Proxy that always forwards to the latest runtime-aware Console."""

    def print(self, *args, **kwargs):
        if is_json_output():
            return None
        return _current_console().print(*args, **kwargs)

    def status(self, *args, **kwargs):
        if is_json_output():
            return _NullStatus()
        return _current_console().status(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(_current_console(), name)


class _NullStatus:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


_CONSOLE_PROXY = ConsoleProxy()


def _current_console() -> Console:
    global _CONSOLE
    if _CONSOLE is None:
        configure_ui_runtime()
    assert _CONSOLE is not None
    return _CONSOLE


def get_console() -> ConsoleProxy:
    return _CONSOLE_PROXY


def current_output_mode() -> str:
    return _resolve_output_mode(_RUNTIME.output_mode)


def is_json_output() -> bool:
    return current_output_mode() == OUTPUT_MODE_JSON


def is_pretty_output() -> bool:
    return current_output_mode() == OUTPUT_MODE_PRETTY


def is_stdout_tty() -> bool:
    return bool(_RUNTIME.stdout_is_tty)


def should_render_rich() -> bool:
    return is_pretty_output() and is_stdout_tty() and not _RUNTIME.no_color


def should_render_banner() -> bool:
    return should_render_rich()


def is_color_disabled() -> bool:
    return bool(_RUNTIME.no_color)


def _set_output_mode_callback(ctx: click.Context, _param: click.Parameter, value: str | None):
    ctx.ensure_object(dict)
    inherited = None
    if ctx.parent is not None and isinstance(ctx.parent.obj, dict):
        inherited = ctx.parent.obj.get("output_mode")

    source = ctx.get_parameter_source("output_mode")
    if source == ParameterSource.DEFAULT:
        selected = inherited or OUTPUT_MODE_PRETTY
    else:
        selected = value or inherited or OUTPUT_MODE_PRETTY

    configure_ui_runtime(output_mode=selected)
    ctx.obj["output_mode"] = selected
    return selected


def _set_no_color_callback(ctx: click.Context, _param: click.Parameter, value: bool):
    ctx.ensure_object(dict)
    inherited = False
    if ctx.parent is not None and isinstance(ctx.parent.obj, dict):
        inherited = bool(ctx.parent.obj.get("no_color"))

    source = ctx.get_parameter_source("no_color")
    selected = inherited if source == ParameterSource.DEFAULT else bool(value)
    configure_ui_runtime(no_color=selected)
    ctx.obj["no_color"] = selected
    return selected


def output_option(
    *,
    supported_modes: tuple[str, ...] = (OUTPUT_MODE_PRETTY, OUTPUT_MODE_JSON),
    hidden: bool = False,
    expose_value: bool = True,
):
    """Add a shared output format option."""
    return click.option(
        "--output",
        "output_mode",
        type=click.Choice(supported_modes, case_sensitive=False),
        default=None,
        callback=_set_output_mode_callback,
        expose_value=expose_value,
        hidden=hidden,
        help="输出格式",
    )


def build_output_click_option(
    *,
    supported_modes: tuple[str, ...] = (OUTPUT_MODE_PRETTY, OUTPUT_MODE_JSON),
    hidden: bool = False,
    expose_value: bool = True,
) -> click.Option:
    """Build a shared output option for command injection."""
    return click.Option(
        ["--output", "output_mode"],
        type=click.Choice(supported_modes, case_sensitive=False),
        default=None,
        callback=_set_output_mode_callback,
        expose_value=expose_value,
        hidden=hidden,
        help="输出格式",
    )


def no_color_option(*, hidden: bool = False, expose_value: bool = False):
    """Add a shared no-color option."""
    return click.option(
        "--no-color",
        "no_color",
        is_flag=True,
        default=False,
        expose_value=expose_value,
        is_eager=True,
        hidden=hidden,
        callback=_set_no_color_callback,
        help="禁用颜色输出",
    )


def build_no_color_click_option(*, hidden: bool = False, expose_value: bool = False) -> click.Option:
    """Build a shared no-color option for command injection."""
    return click.Option(
        ["--no-color", "no_color"],
        is_flag=True,
        default=False,
        expose_value=expose_value,
        is_eager=True,
        hidden=hidden,
        callback=_set_no_color_callback,
        help="禁用颜色输出",
    )


def json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def emit_json(payload: object) -> None:
    click.echo(json_dumps(payload))


@contextmanager
def capture_standard_output(enabled: bool | None = None) -> Iterator[CapturedOutput]:
    """Capture stdout/stderr when machine output must stay clean."""
    should_capture = is_json_output() if enabled is None else bool(enabled)
    if not should_capture:
        yield CapturedOutput()
        return

    captured = CapturedOutput()
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
        yield captured
    captured.stdout = stdout_buffer.getvalue()
    captured.stderr = stderr_buffer.getvalue()


def print_title(title: str, subtitle: str | None = None) -> None:
    if is_json_output():
        return
    console = _current_console()
    if subtitle:
        console.print(f"[title]{title}[/] [muted]{subtitle}[/]")
    else:
        console.print(f"[title]{title}[/]")
    console.print(Rule(style="#30363d"))


def print_rule(label: str | None = None) -> None:
    if is_json_output():
        return
    _current_console().print(Rule(label or "", style="#30363d"))


def print_info(message: str) -> None:
    if is_json_output():
        return
    _current_console().print(f"[muted]{message}[/]")


def print_success(message: str) -> None:
    if is_json_output():
        return
    _current_console().print(f"[ok]{message}[/]")


def print_warn(message: str) -> None:
    if is_json_output():
        return
    _current_console().print(f"[warn]{message}[/]")


def print_error(message: str) -> None:
    if is_json_output():
        return
    _current_console().print(f"[err]{message}[/]")


def print_kv(label: str, value: str, value_style: str = "white", indent: int = 2) -> None:
    if is_json_output():
        return
    space = " " * max(indent, 0)
    _current_console().print(f"{space}[muted]{label}[/]: [{value_style}]{value}[/]")


def print_next_steps(steps: list[str], title: str = "下一步") -> None:
    if is_json_output():
        return
    _current_console().print(f"[title]{title}[/]")
    for step in steps:
        _current_console().print(f"  [muted]•[/] [white]{step}[/]")


def status_rich_style(status: str) -> str:
    return STATUS_RICH_STYLE.get((status or "UNKNOWN").upper(), STATUS_RICH_STYLE["UNKNOWN"])


def status_click_color(status: str) -> str:
    return STATUS_CLICK_COLOR.get((status or "UNKNOWN").upper(), STATUS_CLICK_COLOR["UNKNOWN"])


def replica_rich_style(ready: int, total: int) -> str:
    return "ok" if total > 0 and ready == total else "warn"


def new_table(title: str) -> Table:
    return Table(
        title=f"[title]{title}[/]",
        show_header=True,
        header_style="bold #6e7781",
        border_style="#30363d",
    )


def summary_panel(total: int, healthy: int, attention: int, noun: str) -> Panel:
    body = (
        f"[bold]共 {total} 个 {noun}[/]  "
        f"[ok]健康: {healthy}[/]  "
        f"[warn]待关注: {attention}[/]"
    )
    return Panel.fit(body, border_style="#30363d")


configure_ui_runtime()
