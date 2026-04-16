"""Shared helpers for resource-oriented CLI commands."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Iterable, Sequence, TypeVar

import click

from ksadk.cli.ui import (
    emit_json,
    get_console,
    is_json_output,
    new_table,
    print_info,
    print_kv,
    print_next_steps,
    print_title,
    print_warn,
)

F = TypeVar("F", bound=Callable)

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])
_CONSOLE = get_console()


@dataclass(frozen=True)
class ResourceActionDescriptor:
    """Explicit resource action metadata shared by help, hints and JSON output."""

    name: str
    canonical_command: str
    help_text: str
    kind: str = "read"
    supports_output: bool = True
    supports_dry_run: bool = False
    supports_yes: bool = False
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceActionSet:
    """Legacy canonical action slots kept for compatibility."""

    list: str | None = None
    status: str | None = None
    delete: str | None = None
    deploy: str | None = None
    open: str | None = None
    extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceListSchema:
    """Reusable list presentation contract for a resource."""

    title: str
    noun: str
    columns: tuple[dict, ...]
    empty_message: str
    summary_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceStatusSchema:
    """Reusable detail presentation contract for a resource."""

    title: str
    next_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceDescriptor:
    """Metadata used by resource groups to share help/output conventions."""

    name: str
    summary: str
    actions: ResourceActionSet | tuple[ResourceActionDescriptor, ...]
    resource_key: str | None = None
    list_schema: ResourceListSchema | None = None
    status_schema: ResourceStatusSchema | None = None
    examples: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    missing_ref_message: str | None = None
    resolution_commands: tuple[str, ...] = ()
    list_action_help: str | None = None
    status_action_help: str | None = None
    delete_action_help: str | None = None
    open_action_help: str | None = None
    deploy_action_help: str | None = None
    extra_action_help: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def region_option(
    *,
    default: str | None = "cn-beijing-6",
    envvar: str | None = "KSYUN_REGION",
    help_text: str = "区域",
):
    """Reusable region option."""
    return click.option(
        "--region",
        "-r",
        default=default,
        envvar=envvar,
        show_default=default is not None,
        help=help_text,
    )


def pagination_options(
    *,
    default_page: int = 1,
    default_size: int = 20,
    max_size: int = 100,
):
    """Add standard pagination options."""

    def decorator(func: F) -> F:
        func = click.option(
            "--size",
            default=default_size,
            show_default=True,
            type=click.IntRange(1, max_size),
            help="每页数量",
        )(func)
        func = click.option(
            "--page",
            default=default_page,
            show_default=True,
            type=click.IntRange(1, 10_000),
            help="页码",
        )(func)
        return func

    return decorator


def confirm_options(*, hidden_force_alias: bool = True):
    """Add canonical `--yes` with optional hidden `--force` compatibility."""

    def decorator(func: F) -> F:
        func = click.option(
            "--force",
            "-f",
            "assume_yes",
            is_flag=True,
            hidden=hidden_force_alias,
            help="(兼容) 跳过确认",
        )(func)
        func = click.option(
            "--yes",
            "-y",
            "assume_yes",
            is_flag=True,
            help="跳过确认",
        )(func)
        return func

    return decorator


def confirm_destructive(
    *,
    assume_yes: bool,
    dry_run: bool,
    prompt: str,
    cancel_text: str = "已取消",
) -> bool:
    """Run a standard destructive confirmation flow."""
    if assume_yes or dry_run:
        return True
    if is_json_output():
        from ksadk.cli.error_utils import abort_with_cli_error, usage_error

        abort_with_cli_error(
            usage_error(
                "`--output json` 下 destructive 操作必须显式传入 `--yes`。",
                details={"prompt": prompt},
            )
        )
    if click.confirm(prompt):
        return True
    from ksadk.cli.error_utils import abort_with_cli_error, cancelled_error

    print_info(cancel_text)
    abort_with_cli_error(
        cancelled_error(
            cancel_text,
            details={"prompt": prompt},
        )
    )


def print_list_summary(*, total: int, page: int, size: int, noun: str) -> None:
    """Print a short unified pagination summary."""
    print_info(f"{noun}总数: {total}  页码: {page}  每页: {size}")


def print_compatibility_hint(*, legacy: str, canonical: str) -> None:
    """Print a compact compatibility hint for hidden legacy entrypoints."""
    print_info(f"兼容入口 `{legacy}` 仍可用，推荐改用 `{canonical}`。")


def print_next_action_hint(*commands: str, title: str = "下一步建议") -> None:
    """Print canonical next action hints."""
    normalized = [f"`{cmd}`" for cmd in commands if str(cmd).strip()]
    if normalized:
        print_next_steps(normalized, title=title)


def print_resolution_error(message: str, *commands: str) -> None:
    """Print a standard resolution error with next-step hints."""
    from ksadk.cli.error_utils import emit_cli_error, resolution_error
    from ksadk.cli.ui import print_error

    if is_json_output():
        emit_cli_error(resolution_error(message, hints=list(commands)))
        return
    print_error(message)
    print_next_action_hint(*commands)


def print_resource_resolution_error(
    descriptor: ResourceDescriptor,
    *,
    message: str | None = None,
    commands: Sequence[str] | None = None,
) -> None:
    """Print a canonical resolution error for a resource descriptor."""
    print_resolution_error(
        message or descriptor.missing_ref_message or f"请指定{descriptor.name}",
        *(commands or descriptor.resolution_commands),
    )


def _resource_key(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(text or "").strip()).strip("_").lower()


def get_descriptor_resource_key(descriptor: ResourceDescriptor) -> str:
    return descriptor.resource_key or _resource_key(descriptor.name) or "resource"


def _legacy_action_descriptors(descriptor: ResourceDescriptor) -> tuple[ResourceActionDescriptor, ...]:
    action_set = descriptor.actions
    if not isinstance(action_set, ResourceActionSet):
        return tuple(action_set)

    descriptors: list[ResourceActionDescriptor] = []
    if action_set.list and descriptor.list_action_help:
        descriptors.append(
            ResourceActionDescriptor(
                name="list",
                canonical_command=action_set.list,
                help_text=descriptor.list_action_help,
                kind="read",
                supports_output=True,
                supports_dry_run=False,
            )
        )
    if action_set.status and descriptor.status_action_help:
        descriptors.append(
            ResourceActionDescriptor(
                name="status",
                canonical_command=action_set.status,
                help_text=descriptor.status_action_help,
                kind="read",
                supports_output=True,
                supports_dry_run=False,
            )
        )
    if action_set.delete and descriptor.delete_action_help:
        descriptors.append(
            ResourceActionDescriptor(
                name="delete",
                canonical_command=action_set.delete,
                help_text=descriptor.delete_action_help,
                kind="destructive",
                supports_output=True,
                supports_dry_run=True,
                supports_yes=True,
            )
        )
    if action_set.deploy and descriptor.deploy_action_help:
        descriptors.append(
            ResourceActionDescriptor(
                name="deploy",
                canonical_command=action_set.deploy,
                help_text=descriptor.deploy_action_help,
                kind="write",
                supports_output=True,
                supports_dry_run=True,
            )
        )
    if action_set.open and descriptor.open_action_help:
        descriptors.append(
            ResourceActionDescriptor(
                name="open",
                canonical_command=action_set.open,
                help_text=descriptor.open_action_help,
                kind="interactive",
                supports_output=True,
            )
        )
    for action_name, help_text in descriptor.extra_action_help:
        descriptors.append(
            ResourceActionDescriptor(
                name=action_name,
                canonical_command=action_name,
                help_text=help_text,
                kind="write",
                supports_output=True,
            )
        )
    return tuple(descriptors)


def get_action_descriptors(descriptor: ResourceDescriptor) -> tuple[ResourceActionDescriptor, ...]:
    if isinstance(descriptor.actions, ResourceActionSet):
        return _legacy_action_descriptors(descriptor)
    return tuple(descriptor.actions)


def build_resource_group_help(descriptor: ResourceDescriptor) -> str:
    """Build shared help text for a resource-oriented command group."""
    lines = [descriptor.summary]

    actions = get_action_descriptors(descriptor)
    if actions:
        action_width = max(len(action.name) for action in actions) + 2
        lines.extend(["", "\b", "标准动作:"])
        lines.extend([f"    {action.name:<{action_width}}{action.help_text}" for action in actions])

    if descriptor.examples:
        lines.extend(["", "\b", "示例:"])
        lines.extend([f"    {example}" for example in descriptor.examples])

    if descriptor.notes:
        lines.extend(["", "\b", "说明:"])
        lines.extend([f"    {note}" for note in descriptor.notes])

    return "\n".join(lines)


def build_list_envelope(
    *,
    resource: str,
    items: Sequence[dict[str, Any]],
    page: int,
    size: int,
    total: int,
    hints: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "kind": "list",
        "resource": resource,
        "page": int(page),
        "size": int(size),
        "total": int(total),
        "items": list(items),
        "hints": list(hints or []),
    }


def build_status_envelope(
    *,
    resource: str,
    item: dict[str, Any],
    hints: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "kind": "status",
        "resource": resource,
        "item": dict(item),
        "hints": list(hints or []),
    }


def build_result_envelope(
    *,
    resource: str,
    action: str,
    result: dict[str, Any],
    hints: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "kind": "result",
        "resource": resource,
        "action": action,
        "result": dict(result),
        "hints": list(hints or []),
    }


def build_dry_run_envelope(
    *,
    resource: str,
    action: str,
    request: dict[str, Any],
    hints: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "kind": "dry_run",
        "resource": resource,
        "action": action,
        "request": dict(request),
        "hints": list(hints or []),
    }


def _column_key(column: dict[str, Any], *, index: int) -> str:
    explicit_key = str(column.get("key") or "").strip()
    if explicit_key:
        return explicit_key
    header = str(column.get("header") or f"column_{index + 1}")
    return _resource_key(header) or f"column_{index + 1}"


def _rows_to_items(columns: Sequence[dict], rows: Sequence[Sequence[str]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for idx, cell in enumerate(row):
            column = dict(columns[idx]) if idx < len(columns) else {}
            item[_column_key(column, index=idx)] = cell
        items.append(item)
    return items


def _fields_to_item(fields: Sequence[tuple[str, str, str | None]]) -> dict[str, Any]:
    item: dict[str, Any] = {}
    for idx, (label, value, _style) in enumerate(fields):
        item[_resource_key(label) or f"field_{idx + 1}"] = value
    return item


def render_resource_list(
    *,
    title: str,
    noun: str,
    columns: Sequence[dict],
    rows: Sequence[Sequence[str]],
    total: int,
    page: int,
    size: int,
    empty_message: str,
    summary_lines: Iterable[str] | None = None,
    resource: str = "resource",
    items: Sequence[dict[str, Any]] | None = None,
    hints: Sequence[str] | None = None,
) -> bool:
    """Render a paginated resource list with unified summary output."""
    item_list = list(items) if items is not None else _rows_to_items(columns, rows)
    hint_list = [str(line) for line in (summary_lines or [])]
    if hints:
        hint_list.extend(str(hint) for hint in hints)

    if is_json_output():
        emit_json(
            build_list_envelope(
                resource=resource,
                items=item_list,
                page=page,
                size=size,
                total=total,
                hints=hint_list,
            )
        )
        return bool(item_list)

    if not rows:
        print_warn(empty_message)
        print_list_summary(total=total, page=page, size=size, noun=noun)
        return False

    table = new_table(title)
    for column in columns:
        options = dict(column)
        header = str(options.pop("header"))
        options.pop("key", None)
        table.add_column(header, **options)

    for row in rows:
        table.add_row(*[str(cell) for cell in row])

    _CONSOLE.print(table)
    print_list_summary(total=total, page=page, size=size, noun=noun)
    for line in summary_lines or []:
        print_info(str(line))
    return True


def render_descriptor_list(
    descriptor: ResourceDescriptor,
    *,
    rows: Sequence[Sequence[str]],
    total: int,
    page: int,
    size: int,
    summary_lines: Iterable[str] | None = None,
    items: Sequence[dict[str, Any]] | None = None,
    hints: Sequence[str] | None = None,
) -> bool:
    """Render a resource list from descriptor metadata."""
    if descriptor.list_schema is None:
        raise ValueError(f"{descriptor.name} 未定义列表 schema")
    return render_resource_list(
        title=descriptor.list_schema.title,
        noun=descriptor.list_schema.noun,
        columns=descriptor.list_schema.columns,
        rows=rows,
        total=total,
        page=page,
        size=size,
        empty_message=descriptor.list_schema.empty_message,
        summary_lines=[*descriptor.list_schema.summary_lines, *(summary_lines or [])],
        resource=get_descriptor_resource_key(descriptor),
        items=items,
        hints=hints,
    )


def render_resource_status(
    *,
    title: str,
    subtitle: str | None = None,
    fields: Sequence[tuple[str, str, str | None]],
    next_steps: Sequence[str] | None = None,
    resource: str = "resource",
    action: str = "status",
    item: dict[str, Any] | None = None,
    hints: Sequence[str] | None = None,
) -> None:
    """Render a unified resource detail/status view."""
    hint_list = list(next_steps or [])
    if hints:
        hint_list.extend(str(hint) for hint in hints)
    payload = dict(item or _fields_to_item(fields))

    if is_json_output():
        if action == "status":
            emit_json(
                build_status_envelope(
                    resource=resource,
                    item=payload,
                    hints=hint_list,
                )
            )
        else:
            emit_json(
                build_result_envelope(
                    resource=resource,
                    action=action,
                    result=payload,
                    hints=hint_list,
                )
            )
        return

    print_title(title, subtitle)
    for label, value, value_style in fields:
        if value_style:
            print_kv(label, value, value_style=value_style)
        else:
            print_kv(label, value)
    if next_steps:
        print_next_action_hint(*next_steps)


def render_descriptor_status(
    descriptor: ResourceDescriptor,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    fields: Sequence[tuple[str, str, str | None]],
    next_steps: Sequence[str] | None = None,
    action: str = "status",
    item: dict[str, Any] | None = None,
    hints: Sequence[str] | None = None,
) -> None:
    """Render a resource detail view from descriptor metadata."""
    if descriptor.status_schema is None:
        raise ValueError(f"{descriptor.name} 未定义详情 schema")
    resolved_next_steps = (
        descriptor.status_schema.next_steps
        if next_steps is None and action == "status"
        else tuple(next_steps or ())
    )
    render_resource_status(
        title=title or descriptor.status_schema.title,
        subtitle=subtitle,
        fields=fields,
        next_steps=resolved_next_steps,
        resource=get_descriptor_resource_key(descriptor),
        action=action,
        item=item,
        hints=hints,
    )


class CompatibilityAliasCommand(click.Command):
    """Hidden compatibility alias that only exposes migration help."""

    def __init__(self, *args, canonical_command: str, **kwargs):
        self.canonical_command = canonical_command
        super().__init__(*args, **kwargs)

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, " ".join(self.collect_usage_pieces(ctx)))
        formatter.write_paragraph()
        formatter.write_text("这是兼容入口，建议迁移到新的 canonical 命令。")
        formatter.write_paragraph()
        formatter.write_text(f"推荐命令: {self.canonical_command}")
        formatter.write_text(f"查看帮助: {self.canonical_command} --help")
