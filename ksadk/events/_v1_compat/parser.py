"""v1 wire parser: fold live/replay v1 events into a transcript."""

from __future__ import annotations

import json
from typing import Any, TypeAlias

from ksadk.events._v1_compat.models import EventTypeV1, RuntimeEventV1

_TextKey: TypeAlias = tuple[str, str] | tuple[str, str, str, str, str]
_ToolKey: TypeAlias = str | tuple[str, str, str]
_ArtifactKey: TypeAlias = str | tuple[str, str, str, str]
_TEXT_TYPES = frozenset({EventTypeV1.TEXT_DELTA, EventTypeV1.TEXT_COMPLETED})
_REASONING_TYPES = frozenset({EventTypeV1.REASONING_DELTA, EventTypeV1.REASONING_COMPLETED})
_RUN_TYPES = frozenset(
    {
        EventTypeV1.RUN_STARTED,
        EventTypeV1.RUN_PROGRESS,
        EventTypeV1.RUN_INTERRUPTED,
        EventTypeV1.RUN_COMPLETED,
        EventTypeV1.RUN_FAILED,
        EventTypeV1.RUN_CANCELED,
    }
)


class RuntimeEventV1Parser:
    """Fold v1 live/replay events with identity-aware replace semantics."""

    def __init__(self) -> None:
        self._seen_event_ids: set[str] = set()
        self._text: dict[_TextKey, dict[str, Any]] = {}
        self._reasoning: dict[_TextKey, dict[str, Any]] = {}
        self._tool_calls: dict[_ToolKey, dict[str, Any]] = {}
        self._artifacts: dict[_ArtifactKey, dict[str, Any]] = {}
        self._run_status: dict[str, str] = {}
        self._order: list[tuple[str, Any]] = []
        self._extras: list[dict[str, Any]] = []

    def feed(self, event: RuntimeEventV1) -> None:
        if event.event_id in self._seen_event_ids:
            return
        event.validate_conformance()
        event_type = event.event_type
        if event_type in _TEXT_TYPES:
            self._feed_text(
                self._text,
                "text",
                event,
                final=event_type == EventTypeV1.TEXT_COMPLETED,
            )
        elif event_type in _REASONING_TYPES:
            self._feed_text(
                self._reasoning,
                "reasoning",
                event,
                final=event_type == EventTypeV1.REASONING_COMPLETED,
            )
        elif event_type == EventTypeV1.TOOL_CALL_BEGIN:
            call_id = str(event.payload.get("call_id") or "")
            if call_id:
                tool_key = self._tool_key(event, call_id)
                if tool_key not in self._tool_calls:
                    self._order.append(("tool_call", tool_key))
                self._tool_calls[tool_key] = {
                    "call_id": call_id,
                    "name": event.payload.get("name", ""),
                    "detail": event.payload.get("detail") or {},
                    "done": False,
                    "invocation_id": event.invocation_id,
                    "scope_id": event.payload.get("scope_id"),
                    "item_id": event.payload.get("item_id"),
                    "part_id": event.payload.get("part_id"),
                }
        elif event_type == EventTypeV1.TOOL_CALL_END:
            call_id = str(event.payload.get("call_id") or "")
            if call_id:
                tool_key = self._tool_key(event, call_id)
                if tool_key not in self._tool_calls:
                    self._order.append(("tool_call", tool_key))
                    self._tool_calls[tool_key] = {
                        "call_id": call_id,
                        "name": event.payload.get("name", ""),
                        "detail": {},
                        "done": False,
                        "invocation_id": event.invocation_id,
                        "scope_id": event.payload.get("scope_id"),
                        "item_id": event.payload.get("item_id"),
                        "part_id": event.payload.get("part_id"),
                    }
                self._tool_calls[tool_key]["done"] = True
                self._tool_calls[tool_key]["result"] = event.payload.get("result")
        elif event_type in (EventTypeV1.ARTIFACT_CREATED, EventTypeV1.ARTIFACT_UPDATED):
            name = str(event.payload.get("name") or "artifact")
            artifact_key = self._artifact_key(event, name)
            previous = self._artifacts.get(artifact_key, {"version": 0})
            if artifact_key not in self._artifacts:
                self._order.append(("artifact", artifact_key))
            self._artifacts[artifact_key] = {
                "name": name,
                "version": int(event.payload.get("version") or previous["version"] + 1),
                "text": str(event.payload.get("text") or ""),
                "invocation_id": event.invocation_id,
                "scope_id": event.payload.get("scope_id"),
                "item_id": event.payload.get("item_id"),
                "part_id": event.payload.get("part_id"),
            }
        elif event_type in _RUN_TYPES:
            self._run_status[event.invocation_id] = str(event.payload.get("status") or event_type)
        else:
            self._extras.append(
                {
                    "event_type": event_type,
                    "invocation_id": event.invocation_id,
                    "payload": event.payload,
                }
            )
        self._seen_event_ids.add(event.event_id)

    @staticmethod
    def _identity_triplet(event: RuntimeEventV1) -> tuple[str, str, str] | None:
        values = tuple(event.payload.get(field) for field in ("scope_id", "item_id", "part_id"))
        has_any = any(value is not None for value in values)
        has_all = all(isinstance(value, str) and value for value in values)
        if has_any and not has_all:
            raise ValueError("identity-aware v1 events require scope_id, item_id, and part_id")
        if not has_all:
            return None
        return str(values[0]), str(values[1]), str(values[2])

    def _tool_key(self, event: RuntimeEventV1, call_id: str) -> _ToolKey:
        identity = self._identity_triplet(event)
        if identity is None:
            return call_id
        return event.invocation_id, identity[0], call_id

    def _artifact_key(self, event: RuntimeEventV1, name: str) -> _ArtifactKey:
        identity = self._identity_triplet(event)
        if identity is None:
            return name
        return event.invocation_id, identity[0], identity[1], identity[2]

    def _feed_text(
        self,
        bucket: dict[_TextKey, dict[str, Any]],
        kind: str,
        event: RuntimeEventV1,
        *,
        final: bool,
    ) -> None:
        phase = str(event.phase or "commentary")
        identity_values = tuple(
            event.payload.get(field) for field in ("scope_id", "item_id", "part_id")
        )
        has_any_identity = any(value is not None for value in identity_values)
        has_full_identity = all(isinstance(value, str) and value for value in identity_values)
        if has_any_identity and not has_full_identity:
            raise ValueError("identity-aware v1 text events require scope_id, item_id, and part_id")
        if has_full_identity:
            operation = event.payload.get("operation")
            if operation not in {"append", "replace"}:
                raise ValueError("identity-aware v1 text events require append/replace operation")
            key: _TextKey = (
                event.invocation_id,
                str(identity_values[0]),
                str(identity_values[1]),
                str(identity_values[2]),
                phase,
            )
        else:
            operation = "append"
            key = (event.invocation_id, phase)
        if key not in bucket:
            bucket[key] = {"text": "", "final": False}
            self._order.append((kind, key))
        entry = bucket[key]
        text = str(event.payload.get("text") or "")
        entry["text"] = entry["text"] + text if operation == "append" else text
        if final:
            entry["final"] = True

    def transcript(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for kind, key in self._order:
            if kind in {"text", "reasoning"}:
                bucket = self._text if kind == "text" else self._reasoning
                entry = bucket.get(key, {"text": "", "final": False})
                item = {
                    "kind": kind,
                    "invocation_id": key[0],
                    "phase": key[-1],
                    "text": entry["text"],
                    "final": entry["final"],
                }
                if len(key) == 5:
                    item.update({"scope_id": key[1], "item_id": key[2], "part_id": key[3]})
                items.append(item)
            elif kind == "tool_call":
                call = self._tool_calls.get(key, {})
                item = {
                    "kind": "tool_call",
                    "call_id": call.get("call_id", key),
                    "name": call.get("name", ""),
                    "done": call.get("done", False),
                    "result": call.get("result"),
                    "invocation_id": call.get("invocation_id"),
                }
                if isinstance(key, tuple):
                    item.update(
                        {
                            "scope_id": call.get("scope_id"),
                            "item_id": call.get("item_id"),
                            "part_id": call.get("part_id"),
                        }
                    )
                items.append(item)
            elif kind == "artifact":
                artifact = self._artifacts.get(key, {})
                item = {
                    "kind": "artifact",
                    "name": artifact.get("name", key),
                    "version": artifact.get("version", 1),
                    "text": artifact.get("text", ""),
                    "invocation_id": artifact.get("invocation_id"),
                }
                if isinstance(key, tuple):
                    item.update(
                        {
                            "scope_id": artifact.get("scope_id"),
                            "item_id": artifact.get("item_id"),
                            "part_id": artifact.get("part_id"),
                        }
                    )
                items.append(item)
        return {
            "items": items,
            "run_status": {key: self._run_status[key] for key in sorted(self._run_status)},
            "extras": self._extras,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.transcript(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


__all__ = ["RuntimeEventV1Parser"]
