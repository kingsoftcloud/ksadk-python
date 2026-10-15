"""Resolve previous file publication from original scoped Run effect receipts.

OpenCode task_id and ZCode resumed child sessions preserve continuation origin.
KsADK publication authority remains its own RunStore; immutable disk inventory
alone, child prose and model messages never advance the merge comparison base.
"""

from ksadk.harness.native.ports import ToolResult
from ksadk.harness.workspace_adapters.native_isolation import ChildFilePublication

MERGE_SCHEMA = "native.child-file-merge/v2"


def _confirmed_publication(reader, parent, child):
    cursor = parent
    while True:
        for event in reversed(reader.store.events(cursor.scope, cursor.run_id)):
            if event.kind != "effect.prepared" or event.payload.get("kind") != "tool":
                continue
            operation = event.payload["operation_key"]
            intent = reader.store.get_intent(cursor.scope, cursor.run_id, operation)
            call = (intent.payload.get("call")
                    if intent is not None and intent.kind == "tool" else None)
            if (not isinstance(call, dict) or call.get("name") != "native_child_merge"
                    or call.get("arguments") != {"child_run_id": child.run_id}):
                continue
            receipt = reader.store.get_receipt(cursor.scope, cursor.run_id, operation)
            if receipt is None or receipt.status != "committed":
                continue
            try:
                result = ToolResult.model_validate(receipt.result)
            except ValueError:
                continue
            content = result.content
            if (result.status != "ok" or not isinstance(content, dict)
                    or content.get("schema_version") != MERGE_SCHEMA
                    or content.get("ok") is not True
                    or content.get("child_run_id") != child.run_id
                    or content.get("child_revision") != child.workspace_revision):
                continue
            grant = content.get("publication")
            if (not isinstance(grant, dict) or grant.get("revision") != child.workspace_revision
                    or not isinstance(grant.get("inventory_digest"), str)):
                raise ValueError("child_publication_receipt_invalid")
            return ChildFilePublication(grant["revision"], grant["inventory_digest"])
        if cursor.parent_run_id is None:
            return None
        cursor = reader.store.load(parent.scope, cursor.parent_run_id)
        if cursor.session_id != parent.session_id or not cursor.phase.terminal:
            return None


def previous_child_publication(reader, parent, child):
    """Only a confirmed publication of this child Session's actual ancestor."""
    source_id = child.parent_run_id
    while source_id is not None:
        resolved = reader._load_session_child(parent, source_id)
        if resolved is None:
            return None
        source, _source_parent = resolved
        if (source.scope != child.scope or source.session_id != child.session_id
                or source.origin.get("workspace_isolation_id")
                != child.origin.get("workspace_isolation_id")):
            return None
        if grant := _confirmed_publication(reader, parent, source):
            return grant
        source_id = source.parent_run_id
    return None
