# -*- coding: utf-8 -*-
"""ksadk replay — 基于 RuntimeEvent store + session 级订阅的历史回放 (goal-16)。

CLI 是消费 RuntimeAdapter/RuntimeEvent 的薄壳(不含 runtime 逻辑):replay 复用 goal-10
``RuntimeEventStore`` 与 goal-12 共享 parser/replay,把某 session 的 RuntimeEvent 历史
回放为确定性 transcript(text/reasoning/tool/artifact/run + a2ui/a2a extras)。

注:回放的是经 RuntimeEvent 持久化的新事件模型(带 runtime marker);legacy SessionEvent
(旧 assistant_message 等)不在本模型内,不回放。
"""

from __future__ import annotations

import asyncio
import json

import click

from ksadk.cli.resource_common import CONTEXT_SETTINGS
from ksadk.events.replay import replay_transcript
from ksadk.events.store import RuntimeEventStore

_HELP = dict(help_option_names=["-h", "--help"])


@click.command(
    "replay", context_settings=CONTEXT_SETTINGS, help="回放某 session 的 RuntimeEvent 历史"
)
@click.argument("session_id")
@click.option(
    "--after-seq-id", default=0, show_default=True, type=int, help="从该 seq cursor 之后回放"
)
@click.option("--before-seq-id", default=None, type=int, help="回放上界(不含该 seq)")
@click.option(
    "--format", "fmt", type=click.Choice(["json", "text"]), default="text", show_default=True
)
def replay(session_id: str, after_seq_id: int, before_seq_id: int | None, fmt: str):
    """回放 session 的 RuntimeEvent 历史(text/reasoning/tool/artifact/run)。"""
    asyncio.run(_run(session_id, after_seq_id=after_seq_id, before_seq_id=before_seq_id, fmt=fmt))


async def _run(session_id: str, *, after_seq_id: int, before_seq_id: int | None, fmt: str) -> None:
    from ksadk.sessions import resolve_session_service

    store = RuntimeEventStore(resolve_session_service())
    parser = await replay_transcript(
        store, session_id, after_seq_id=after_seq_id, before_seq_id=before_seq_id
    )
    transcript = parser.transcript()
    if fmt == "json":
        click.echo(json.dumps(transcript, ensure_ascii=False, sort_keys=True))
        return

    items = transcript["items"]
    if not items and not transcript["extras"]:
        click.echo(f"session {session_id} 无 RuntimeEvent 历史(或该 session 未经新事件模型持久化)")
        return
    for item in items:
        kind = item.get("kind")
        if kind in ("text", "reasoning"):
            marker = "▍final" if item.get("final") else "▍delta"
            click.echo(f"[{kind}/{item.get('phase')}] {marker} {item.get('text')}")
        elif kind == "tool_call":
            done = "✓" if item.get("done") else "…"
            click.echo(f"[tool] {done} {item.get('name')} ({item.get('call_id')})")
        elif kind == "artifact":
            click.echo(f"[artifact] {item.get('name')} v{item.get('version')}")
    for inv, status in transcript["run_status"].items():
        click.echo(f"[run] {inv}: {status}")
    for extra in transcript["extras"]:
        click.echo(f"[{extra['event_type']}] {json.dumps(extra['payload'], ensure_ascii=False)}")
