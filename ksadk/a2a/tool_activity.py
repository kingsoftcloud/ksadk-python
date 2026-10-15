"""Explicit public tool disclosure at the callee wire boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from a2a.types import Part
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
from pydantic import BaseModel, ConfigDict, Field

from ksadk.a2a.presentation import (
    A2A_TOOL_ACTIVITY_EXTENSION_URI,
    A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES,
    A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES,
    build_public_tool_activity,
)
from ksadk.events.canonical import ItemCompleted, ItemSnapshotReplaced, ItemStarted, ItemUpdated
from ksadk.events.content import ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_item_id


@dataclass(frozen=True)
class PublicToolRule:
    """Only named top-level fields cross the boundary; everything else is private."""

    name: str
    arguments: frozenset[str] = frozenset()
    results: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PublicToolActivityPolicy:
    tools: Mapping[str, PublicToolRule] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: str) -> PublicToolActivityPolicy:
        """Load a platform-owned declaration, never a request/model override."""
        declaration = _PublicToolDeclaration.model_validate_json(payload)
        return cls(
            {
                name: PublicToolRule(rule.name, frozenset(rule.arguments), frozenset(rule.results))
                for name, rule in declaration.tools.items()
            }
        )


class _PublicToolFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
    arguments: list[str] = Field(default_factory=list, max_length=100)
    results: list[str] = Field(default_factory=list, max_length=100)


class _PublicToolDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tools: dict[str, _PublicToolFields] = Field(max_length=100)


class PublicToolActivityProducer:
    def __init__(self, policy: PublicToolActivityPolicy) -> None:
        self.policy = policy
        self._calls: dict[tuple[str, str], PublicToolRule] = {}
        self._bytes = 0
        self._seen: set[str] = set()
        self._budget_exhausted = False

    async def _publish(
        self,
        *,
        activity_payload: dict[str, Any],
        public_item: str,
        public_event: str,
        updater: Any,
    ) -> None:
        if public_event in self._seen or self._budget_exhausted:
            return
        activity = build_public_tool_activity(activity_payload)
        size = len(activity.model_dump_json().encode())
        # Reserve room for an explicit, sanitized truncation observation.
        # Never consume the entire invocation budget and silently drop output.
        exhausted = self._bytes + size > (
            A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES - A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES
        )
        if exhausted:
            activity = build_public_tool_activity(
                {**activity_payload, "arguments": None, "result": None, "truncated": True}
            )
            size = len(activity.model_dump_json().encode())
        terminal = activity.operation in {"completed", "failed"}
        await updater.add_artifact(
            artifact_id=public_item,
            name="public-tool-activity",
            parts=[
                Part(
                    data=ParseDict(activity.model_dump(mode="json"), Value()),
                    metadata={"extension_uri": A2A_TOOL_ACTIVITY_EXTENSION_URI},
                )
            ],
            metadata={
                "event_id": public_event,
                "extension_uri": A2A_TOOL_ACTIVITY_EXTENSION_URI,
            },
            append=False,
            last_chunk=terminal,
        )
        # A failed store/write is retryable with the same public event identity.
        self._bytes += size
        self._seen.add(public_event)
        self._budget_exhausted = exhausted

    async def emit(self, event: Any, updater: Any) -> None:
        if not isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted)):
            return
        if event.item_kind not in {"tool_call", "tool_result"}:
            return
        snapshot = getattr(event, "snapshot", None) or getattr(event, "initial", None)
        parts = snapshot.parts if snapshot is not None else (getattr(event, "update", None),)
        for part in parts:
            if isinstance(part, ToolCallContent):
                rule = self.policy.tools.get(part.name)
                if rule is None:
                    continue
                self._calls[(event.scope_id, part.call_id)] = rule
            elif isinstance(part, ToolResultContent):
                rule = self._calls.get((event.scope_id, part.call_id))
                if rule is None:
                    continue
            else:
                continue
            public_item = stable_item_id("a2a-tool", event.scope_id, event.item_id)
            public_call = stable_item_id("a2a-tool", event.scope_id, part.call_id)
            is_call = isinstance(part, ToolCallContent)
            is_error = isinstance(part, ToolResultContent) and part.is_error
            complete = isinstance(event, ItemCompleted)
            operation = "failed" if is_error else "completed" if complete else "updated"
            common = {
                "item_id": public_item,
                "call_id": public_call,
                "item_kind": "tool_call" if is_call else "tool_result",
                "execution_status": (
                    "failed" if is_error else "completed" if complete and not is_call else "running"
                ),
                "name": rule.name if is_call else None,
                "arguments": self._select(part.arguments, rule.arguments) if is_call else None,
                "result": None if is_call else self._select(part.result, rule.results),
                "is_error": is_error,
            }
            if is_call and complete:
                started_event = stable_item_id("a2a-tool", event.event_id, part.part_id, "started")
                await self._publish(
                    activity_payload={
                        **common,
                        "event_id": started_event,
                        "operation": "started",
                        "execution_status": "running",
                    },
                    public_item=public_item,
                    public_event=started_event,
                    updater=updater,
                )
            public_event = stable_item_id("a2a-tool", event.event_id, part.part_id, operation)
            await self._publish(
                activity_payload={**common, "event_id": public_event, "operation": operation},
                public_item=public_item,
                public_event=public_event,
                updater=updater,
            )

    @staticmethod
    def _select(value: Any, keys: frozenset[str]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        return {key: value[key] for key in keys if key in value}
