"""Resolve an accounted child Session through its original scoped receipts."""

from ksadk.harness.native.subagents import ChildRole, ChildRunRequest
from ksadk.harness.stores.native_contract import RunStore

from .native_child_result import NativeChildResultPort


def bind_child_continuation(
    child: ChildRunRequest,
    source_run_id: str,
    *,
    store: RunStore,
    roles: tuple[ChildRole, ...],
) -> ChildRunRequest:
    """Keep the new operation's identity and grants, and reuse only the Session."""
    if not isinstance(source_run_id, str) or not source_run_id.strip() or len(source_run_id) > 256:
        raise ValueError("child_arguments_invalid")
    parent = store.load(child.parent_scope, child.parent_run_id)
    reader = NativeChildResultPort(scope=child.parent_scope, store=store, roles=roles)
    resolved = reader._load_session_child(parent, source_run_id)
    if resolved is None:
        raise ValueError("child_followup_not_available")
    source, source_parent = resolved
    if source.scope != child.scope:
        raise ValueError("child_followup_not_available")
    if source.origin["role"] != child.role.role_id:
        raise ValueError("child_followup_role_mismatch")
    allocation = next(
        (
            item
            for item in source_parent.child_budget_reservations
            if item.child_run_id == source.run_id
        ),
        None,
    )
    # A sealed foreground receipt has no allocation; the existing resolver
    # admits it only after its actual descendant usage has been accounted.
    if not source.phase.terminal or (
        allocation is not None and allocation.settled_usage_digest is None
    ):
        raise ValueError("child_followup_requires_result_collection")
    return ChildRunRequest.model_validate(
        child.model_copy(
            update={
                "child_session_id": source.session_id,
                "continuation_of": source.run_id,
                "continuation_parent_run_id": source_parent.run_id,
            }
        ).model_dump(mode="json")
    )
