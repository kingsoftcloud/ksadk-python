"""Local observability commands."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from ksadk.cli.ui import configure_ui_runtime, emit_json, is_json_output, print_kv, print_success
from ksadk.observability.session_log import SessionLogError, export_session_log
from ksadk.sessions.local_service import LocalSessionService


class ObserveCliError(click.ClickException):
    exit_code = 2


@click.group("observe", context_settings=dict(help_option_names=["-h", "--help"]))
def observe() -> None:
    """查询和导出本地 Agent 观测数据。"""


async def _export(
    session_id: str,
    output_path: Path,
    invocation_id: str | None,
):
    service = LocalSessionService(project_dir=str(Path.cwd()))
    try:
        return await export_session_log(
            service,
            session_id,
            output_path,
            invocation_id=invocation_id,
        )
    finally:
        await service.aclose()


@observe.command("export")
@click.option("--session-id", required=True)
@click.option("--invocation-id")
@click.option(
    "--output",
    "output_path",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["pretty", "json"]),
    default="pretty",
    show_default=True,
)
def export_command(
    session_id: str,
    invocation_id: str | None,
    output_path: Path,
    output_format: str,
) -> None:
    """将本地 Session 事件导出为可校验 JSONL。"""
    configure_ui_runtime(output_mode=output_format)
    try:
        result = asyncio.run(_export(session_id, output_path, invocation_id))
    except SessionLogError as exc:
        raise ObserveCliError(str(exc)) from exc

    payload = {
        "path": str(result.path),
        "eventCount": result.event_count,
        "firstSeqId": result.first_seq_id,
        "lastSeqId": result.last_seq_id,
        "exportedThroughSeqId": result.exported_through_seq_id,
    }
    if is_json_output():
        emit_json(payload)
        return
    print_success(f"已导出 {result.event_count} 条事件")
    print_kv("文件", str(result.path))
    print_kv("序号范围", f"{result.first_seq_id or '-'} - {result.last_seq_id or '-'}")


__all__ = ["observe"]
