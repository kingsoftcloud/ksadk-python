"""共享 RuntimeEvent → transcript parser (goal-12,H2 §3.2 P1-N)。

**单实现**:live 增量渲染与 replay 历史回放**共用同一个 parser**,从根上杜绝"两个仓/
两条路各实现一遍导致的行为漂移"(H2 高风险:历史 replay 行为漂移)。

parser 把一串 :class:`RuntimeEvent` 折叠成**确定性** transcript(按事件顺序的 item 列表
+ run 状态),``to_json`` 输出逐字节稳定(json sort_keys + 有序 item),供 conformance
fixture 断言 live 渲染与 replay 渲染**逐字节一致**。

设计要点:

- text/reasoning:按 ``(invocation_id, phase)`` 分组累积 delta,``completed`` 收尾。
- tool.call:``begin`` 开工、``end`` 收尾(同名 call_id 配对)。
- artifact:``created``/``updated`` 按 name 登记/更新版本。
- run.*:按 invocation 记录最新 run 状态。
- checkpoint/usage/a2ui/a2a:记为带类型的附加项(保序,不丢事件)。
"""

from __future__ import annotations

import json
from typing import Any

from ksadk.events.runtime_event import EventType, RuntimeEvent

#: parser 消费的渲染族(其余事件类型记为 generic 附加项,不丢)。
_TEXT_TYPES = frozenset({EventType.TEXT_DELTA, EventType.TEXT_COMPLETED})
_REASONING_TYPES = frozenset({EventType.REASONING_DELTA, EventType.REASONING_COMPLETED})
_RUN_TYPES = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.RUN_PROGRESS,
        EventType.RUN_INTERRUPTED,
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELED,
    }
)


class RuntimeEventParser:
    """RuntimeEvent → 确定性 transcript 的共享 parser(live/replay 单实现)。"""

    def __init__(self) -> None:
        # (invocation_id, phase) -> {"text": str, "final": bool}
        self._text: dict[tuple[str, str], dict[str, Any]] = {}
        self._reasoning: dict[tuple[str, str], dict[str, Any]] = {}
        # call_id -> {"name","detail","done"}
        self._tool_calls: dict[str, dict[str, Any]] = {}
        # name -> {"version","text"}
        self._artifacts: dict[str, dict[str, Any]] = {}
        # invocation_id -> 最新 run 状态字符串
        self._run_status: dict[str, str] = {}
        # 渲染顺序:text/reasoning/tool/artifact 首次出现的键
        self._order: list[tuple[str, Any]] = []
        # 其他事件(checkpoint/usage/a2ui/a2a)保序附加
        self._extras: list[dict[str, Any]] = []

    # ---- 增量喂事件(live 与 replay 同一条路径) ----

    def feed(self, event: RuntimeEvent) -> None:
        et = event.event_type
        if et in _TEXT_TYPES:
            self._feed_text(self._text, "text", event, final=(et == EventType.TEXT_COMPLETED))
        elif et in _REASONING_TYPES:
            self._feed_text(
                self._reasoning, "reasoning", event, final=(et == EventType.REASONING_COMPLETED)
            )
        elif et == EventType.TOOL_CALL_BEGIN:
            call_id = str(event.payload.get("call_id") or "")
            if call_id:
                self._tool_calls[call_id] = {
                    "name": event.payload.get("name", ""),
                    "detail": event.payload.get("detail") or {},
                    "done": False,
                    "invocation_id": event.invocation_id,
                }
                self._order.append(("tool_call", call_id))
        elif et == EventType.TOOL_CALL_END:
            call_id = str(event.payload.get("call_id") or "")
            if call_id and call_id in self._tool_calls:
                self._tool_calls[call_id]["done"] = True
                self._tool_calls[call_id]["result"] = event.payload.get("result")
        elif et in (EventType.ARTIFACT_CREATED, EventType.ARTIFACT_UPDATED):
            name = str(event.payload.get("name") or "artifact")
            prev = self._artifacts.get(name, {"version": 0})
            if name not in self._artifacts:
                self._order.append(("artifact", name))
            self._artifacts[name] = {
                "version": int(event.payload.get("version") or prev["version"] + 1),
                "text": str(event.payload.get("text") or ""),
                "invocation_id": event.invocation_id,
            }
        elif et in _RUN_TYPES:
            status = str(event.payload.get("status") or et)
            self._run_status[event.invocation_id] = status
        else:
            # checkpoint/usage/a2ui/a2a 等:保序附加,不丢事件。
            self._extras.append(
                {
                    "event_type": et,
                    "invocation_id": event.invocation_id,
                    "payload": event.payload,
                }
            )

    def _feed_text(
        self,
        bucket: dict[tuple[str, str], dict[str, Any]],
        kind: str,
        event: RuntimeEvent,
        *,
        final: bool,
    ) -> None:
        key = (event.invocation_id, str(event.phase or "commentary"))
        entry = bucket.setdefault(key, {"text": "", "final": False})
        if (
            len(bucket) == 1
            and entry["text"] == ""
            and not any(k == (kind, key) for k in self._order)
        ):
            self._order.append((kind, key))
        entry["text"] += str(event.payload.get("text") or "")
        if final:
            entry["final"] = True

    # ---- 投影 ----

    def transcript(self) -> dict[str, Any]:
        """折叠为确定性 transcript(dict;``to_json`` 逐字节稳定)。"""
        items: list[dict[str, Any]] = []
        for kind, key in self._order:
            if kind == "text":
                entry = self._text.get(key, {"text": "", "final": False})
                items.append(
                    {
                        "kind": "text",
                        "invocation_id": key[0],
                        "phase": key[1],
                        "text": entry["text"],
                        "final": entry["final"],
                    }
                )
            elif kind == "reasoning":
                entry = self._reasoning.get(key, {"text": "", "final": False})
                items.append(
                    {
                        "kind": "reasoning",
                        "invocation_id": key[0],
                        "phase": key[1],
                        "text": entry["text"],
                        "final": entry["final"],
                    }
                )
            elif kind == "tool_call":
                call = self._tool_calls.get(key, {})
                items.append(
                    {
                        "kind": "tool_call",
                        "call_id": key,
                        "name": call.get("name", ""),
                        "done": call.get("done", False),
                        "result": call.get("result"),
                        "invocation_id": call.get("invocation_id"),
                    }
                )
            elif kind == "artifact":
                art = self._artifacts.get(key, {})
                items.append(
                    {
                        "kind": "artifact",
                        "name": key,
                        "version": art.get("version", 1),
                        "text": art.get("text", ""),
                        "invocation_id": art.get("invocation_id"),
                    }
                )
        return {
            "items": items,
            "run_status": {k: self._run_status[k] for k in sorted(self._run_status)},
            "extras": self._extras,
        }

    def to_json(self) -> str:
        """确定性 JSON 序列化(sort_keys + 紧凑分隔符),供 conformance 逐字节比对。"""
        return json.dumps(
            self.transcript(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


__all__ = ["RuntimeEventParser"]
