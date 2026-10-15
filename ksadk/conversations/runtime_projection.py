"""Run-local presentation derived exclusively from canonical runtime facts."""

from __future__ import annotations

import json
from typing import Literal

from ksadk.a2a.presentation import ExecutionScopeDescriptor
from ksadk.conversations.contracts import ConversationItem
from ksadk.conversations.projector import project_conversation_item
from ksadk.events.canonical import (
    InteractionRequested,
    InteractionResolved,
    RunCompleted,
    RuntimeEvent,
)
from ksadk.events.content import DataContent, TextContent, ToolResultContent
from ksadk.events.reducer import StreamReducer


def conversation_identity(run: str, scope: str, item: str) -> str:
    return json.dumps([run, scope, item], ensure_ascii=False, separators=(",", ":"))


class RuntimeConversationProjection:
    """Replay the prefix before projecting a cursor suffix; never guess child text.

    Instances belong to one run and one negotiated profile. They contain only
    a derived view, and must be rebuilt from the authoritative event store after
    a process restart or reconnect.
    """

    def __init__(
        self,
        session_id: str,
        *,
        profile: Literal["flat-v1", "agent-block-v1"],
        kernel_interactions: bool = False,
    ):
        if profile not in {"flat-v1", "agent-block-v1"}:
            raise ValueError("unsupported presentation profile")
        self.session_id, self.profile = session_id, profile
        self.runtime = StreamReducer()
        self._parents: dict[str, str] = {}
        self._descriptors: dict[str, str] = {}
        self._kernel_interactions = kernel_interactions
        self._interactions: dict[tuple[str, str], str] = {}

    def _identity(self, event: RuntimeEvent, scope: str, item: str) -> str:
        if self.profile == "agent-block-v1" or scope in self._parents:
            return conversation_identity(event.run_id, scope, item)
        return item

    def apply(self, event: RuntimeEvent) -> ConversationItem | None:
        if not self.runtime.apply(event).applied:
            return None
        if event.parent_scope_id:
            self._parents[event.scope_id] = event.parent_scope_id
        if (
            isinstance(event, (InteractionRequested, InteractionResolved))
            and event.scope_id in self._parents
        ):
            key = (event.run_id, event.interaction_id)
            previous = self._interactions.get(key)
            if previous is not None and previous != event.scope_id:
                raise ValueError("interaction scope changed")
            self._interactions[key] = event.scope_id
            if self._kernel_interactions:
                # The native fact establishes scope, but only the ledger's
                # revision can authorize a user submission.
                return None
        projected = project_conversation_item(event, session_id=self.session_id)
        view = self.runtime.snapshot()
        native_item = getattr(event, "item_id", None)
        item = next(
            (i for i in view.items if (i.scope_id, i.item_id) == (event.scope_id, native_item)),
            None,
        )
        if (
            item
            and event.scope_id in self._parents
            and item.item_kind == "status"
            and item.status == "open"
            and not item.parts
        ):
            return None
        fields = projected.model_dump()
        if item and event.scope_id in self._parents:
            if item.item_kind in {"artifact", "tool_call", "tool_result"} and not item.parts:
                # Native A2A declares identity before the first typed part. Do
                # not expose an empty artifact/tool with an invented payload.
                return None
            if item.item_kind == "artifact" and all(isinstance(p, TextContent) for p in item.parts):
                # A2A transports ordinary response text as an Artifact. The
                # canonical kind stays artifact; the presentation renders its
                # typed text parts instead of dropping them as an unknown file.
                fields.update(
                    kind="assistant_text",
                    payload_schema_ref="conversation.item.assistant_text/v1",
                    payload={"text": "".join(p.text for p in item.parts)},
                    operation="completed" if item.status == "completed" else "replace",
                    lifecycle="completed" if item.status == "completed" else "streaming",
                )
        fields["item_id"] = self._identity(event, event.scope_id, projected.item_id)
        fields["native_ref"].update(scopeId=event.scope_id, runtimeItemId=native_item)
        if event.scope_id in self._parents:
            fields["native_ref"]["parentScopeId"] = self._parents[event.scope_id]
            parent = self._descriptors.get(event.scope_id)
            if parent:
                fields["parent_item_id"] = self._identity(event, event.scope_id, parent)
            if self.profile == "flat-v1" or projected.kind == "reasoning":
                fields["visibility"] = "hidden"
        descriptor_data = (
            next(
                (
                    p.data
                    for p in item.parts
                    if isinstance(p, DataContent)
                    and isinstance(p.data, dict)
                    and p.data.get("schema") == "execution.scope/v1"
                ),
                None,
            )
            if item
            else None
        )
        if descriptor_data is not None:
            descriptor = ExecutionScopeDescriptor.model_validate(descriptor_data)
            self._descriptors[event.scope_id] = item.item_id
            self._parents[event.scope_id] = descriptor.parent_scope_id
            if self.profile == "agent-block-v1":
                terminal = descriptor.status in {"completed", "failed", "cancelled"}
                fields.update(
                    kind="agent",
                    payload_schema_ref="conversation.item.agent/v1",
                    capability_ref="agent.block",
                    payload=descriptor.model_dump(mode="json", by_alias=True),
                    parent_item_id=self._identity(
                        event, descriptor.trigger_ref.scope_id, descriptor.trigger_ref.item_id
                    ),
                    lifecycle="failed"
                    if descriptor.status == "failed"
                    else "completed"
                    if terminal
                    else "streaming",
                    operation="completed" if terminal else "replace",
                )
            else:
                fields["visibility"] = "hidden"
        if item and item.item_kind in {"tool_call", "tool_result"}:
            result = next((p for p in item.parts if isinstance(p, ToolResultContent)), None)
            status = (
                "failed"
                if item.status == "failed" or (result and result.is_error)
                else ("completed" if result and item.status == "completed" else "running")
            )
            fields["payload"].update(executionStatus=status, sourceKind=item.item_kind)
        if isinstance(event, RunCompleted) and self.profile == "flat-v1":
            # Pure delegation must provide explicit root output refs. Preserve
            # an existing root answer; never aggregate arbitrary child output.
            root_answer = any(
                i.scope_id not in self._parents
                and i.phase == "final_answer"
                and i.status == "completed"
                for i in view.items
            )
            if (
                not root_answer
                and event.output_refs
                and all(ref.scope_id in self._parents for ref in event.output_refs)
            ):
                texts = []
                for ref in event.output_refs:
                    target = next(
                        i
                        for i in view.items
                        if (i.scope_id, i.item_id) == (ref.scope_id, ref.item_id)
                    )
                    texts.extend(
                        p.text
                        for p in target.parts
                        if isinstance(p, TextContent)
                        and (ref.part_id is None or p.part_id == ref.part_id)
                    )
                fields.update(
                    item_id=conversation_identity(event.run_id, event.scope_id, "delegated-final"),
                    kind="assistant_text",
                    payload_schema_ref="conversation.item.assistant_text/v1",
                    operation="completed",
                    lifecycle="completed",
                    payload={"text": "".join(texts)},
                )
        return ConversationItem.model_validate(fields)

    def scope_ledger_item(self, item: ConversationItem) -> ConversationItem:
        """Attach native child identity to the one actionable ledger projection."""
        if item.native_ref.get("protocol") != "agent-kernel/interaction-v1":
            return item
        interaction_id = item.payload.get("interactionId")
        scope = self._interactions.get((item.run_id, interaction_id))
        if scope is None:
            return item
        parent = self._descriptors.get(scope)
        return item.model_copy(
            update={
                "item_id": conversation_identity(item.run_id, scope, interaction_id),
                "parent_item_id": conversation_identity(item.run_id, scope, parent)
                if parent and self.profile == "agent-block-v1"
                else None,
                "native_ref": {
                    **item.native_ref,
                    "scopeId": scope,
                    "parentScopeId": self._parents[scope],
                    "runtimeItemId": interaction_id,
                },
            }
        )
