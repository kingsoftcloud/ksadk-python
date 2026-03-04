"""CLI 公共 UI 组件与主题。

提供统一的 Console、状态配色、表格样式和摘要面板，供各命令复用。
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.theme import Theme

STATUS_RICH_STYLE = {
    "RUNNING": "bold #2da44e",
    "READY": "bold #2da44e",
    "HEALTHY": "bold #2da44e",
    "CREATING": "bold #d29922",
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
_CONSOLE = Console(theme=_THEME)


def get_console() -> Console:
    return _CONSOLE


def print_title(title: str, subtitle: str | None = None) -> None:
    if subtitle:
        _CONSOLE.print(f"[title]{title}[/] [muted]{subtitle}[/]")
    else:
        _CONSOLE.print(f"[title]{title}[/]")
    _CONSOLE.print(Rule(style="#30363d"))


def print_rule(label: str | None = None) -> None:
    _CONSOLE.print(Rule(label or "", style="#30363d"))


def print_info(message: str) -> None:
    _CONSOLE.print(f"[muted]{message}[/]")


def print_success(message: str) -> None:
    _CONSOLE.print(f"[ok]{message}[/]")


def print_warn(message: str) -> None:
    _CONSOLE.print(f"[warn]{message}[/]")


def print_error(message: str) -> None:
    _CONSOLE.print(f"[err]{message}[/]")


def print_kv(label: str, value: str, value_style: str = "white", indent: int = 2) -> None:
    space = " " * max(indent, 0)
    _CONSOLE.print(f"{space}[muted]{label}[/]: [{value_style}]{value}[/]")


def print_next_steps(steps: list[str], title: str = "下一步") -> None:
    _CONSOLE.print(f"[title]{title}[/]")
    for step in steps:
        _CONSOLE.print(f"  [muted]•[/] [white]{step}[/]")


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
