"""历史 replay — 基于 session 级 cursor 的跨 invocation 回放 (goal-12)。

replay 与 live 渲染**共用** :class:`~ksadk.events.parser.RuntimeEventParser`(单实现),
回放只是"从 store 按 cursor 读事件、喂同一个 parser"。因此 live 与 replay 不可能漂移
(H2 高风险项),由 conformance fixture 逐字节断言兜底。
"""

from __future__ import annotations

from typing import Optional

from ksadk.events.parser import RuntimeEventParser
from ksadk.events.store import RuntimeEventStore


async def replay_transcript(
    store: RuntimeEventStore,
    session_id: str,
    *,
    after_seq_id: int = 0,
    before_seq_id: Optional[int] = None,
    parser: Optional[RuntimeEventParser] = None,
) -> RuntimeEventParser:
    """跨 invocation 历史回放:按 session cursor 读事件,经共享 parser 折叠成 transcript。

    与 live 渲染同一条 parser 路径——live 是"事件边来边 feed",replay 是"从 store 读完
    再 feed 同一个 parser",产物逐字节一致(conformance fixture 证明)。
    """
    parser = parser or RuntimeEventParser()
    events = await store.list(session_id, after_seq_id=after_seq_id, before_seq_id=before_seq_id)
    for event in events:
        event.validate_conformance()
        parser.feed(event)
    return parser


__all__ = ["replay_transcript"]
