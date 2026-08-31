from __future__ import annotations

import pytest

from ksadk.harness.context_maintenance import (
    ContextMaintenanceQueue,
    plan_context_maintenance,
)
from ksadk.harness.state import WorkingContext
from ksadk.harness.working_context_patch import (
    WorkingContextVersionError,
    apply_patch,
)


def test_maintenance_returns_auditable_patch_without_mutating_input():
    working = WorkingContext(
        version=7,
        goal="must stay untouched",
        plan="stable session plan",
        confirmed_constraints=(" use CNY ", "", "USE CNY", "final answer in Chinese"),
        verified_facts=("invoice AP-1", " invoice   AP-1 ", "amount 42"),
        recent_tool_failures=("lookup: timeout", "LOOKUP: TIMEOUT"),
    )

    planned = plan_context_maintenance(working, source_event_ids=("evt-2", "evt-2"))

    assert planned.patch is not None
    assert planned.patch.base_version == 7
    assert planned.patch.source_event_ids == ("evt-2",)
    assert planned.patch.reason == "background_context_maintenance"
    assert planned.removed_empty == 1
    assert planned.removed_duplicates == 3
    assert working.version == 7
    assert working.goal == "must stay untouched"

    maintained = apply_patch(working, planned.patch)
    assert maintained.version == 8
    assert maintained.goal == working.goal
    assert maintained.plan == working.plan
    assert maintained.confirmed_constraints == ("USE CNY", "final answer in Chinese")
    assert maintained.verified_facts == ("invoice AP-1", "amount 42")
    assert maintained.recent_tool_failures == ("LOOKUP: TIMEOUT",)


def test_clean_context_produces_no_patch():
    planned = plan_context_maintenance(
        WorkingContext(version=2, verified_facts=("fact one", "fact two"))
    )

    assert planned.patch is None
    assert planned.to_dict()["changed"] is False


def test_background_patch_never_silently_overwrites_newer_state():
    original = WorkingContext(version=1, verified_facts=("old", " OLD "))
    patch = plan_context_maintenance(original).patch
    assert patch is not None
    newer = original.model_copy(update={"version": 2, "verified_facts": ("new",)})

    with pytest.raises(WorkingContextVersionError, match="version conflict"):
        apply_patch(newer, patch)


def test_maintenance_report_contains_no_stable_prompt_or_message_content():
    planned = plan_context_maintenance(
        WorkingContext(confirmed_constraints=("same", "SAME"))
    ).to_dict()

    assert "stable_prompt" not in str(planned).lower()
    assert "messages" not in str(planned).lower()


class _SnapshotStore:
    def __init__(self, value: WorkingContext | None, *, conflict: bool = False):
        self.value = value
        self.conflict = conflict
        self.writes = 0

    def load(self, *, tenant_id, session_id):
        return self.value

    def compare_and_set(
        self, *, tenant_id, session_id, expected_version, value
    ):
        if self.conflict or self.value is None or self.value.version != expected_version:
            return False
        self.value = value
        self.writes += 1
        return True


def test_background_queue_coalesces_and_applies_with_compare_and_set():
    store = _SnapshotStore(
        WorkingContext(version=3, confirmed_constraints=("same", " SAME "))
    )
    queue = ContextMaintenanceQueue(store)
    task = queue.submit(tenant_id="tenant-a", session_id="session-a")
    duplicate = queue.submit(tenant_id="tenant-a", session_id="session-a")

    completed = queue.run_pending(limit=1)[0]

    assert duplicate.task_id == task.task_id
    assert completed.status == "succeeded"
    assert store.writes == 1
    assert store.value is not None
    assert store.value.version == 4
    assert store.value.confirmed_constraints == ("SAME",)


def test_background_queue_never_overwrites_a_conflict():
    store = _SnapshotStore(
        WorkingContext(version=3, confirmed_constraints=("same", "SAME")),
        conflict=True,
    )
    queue = ContextMaintenanceQueue(store)
    queue.submit(tenant_id="tenant-a", session_id="session-a")

    completed = queue.run_pending(limit=1)[0]

    assert completed.status == "conflicted"
    assert completed.error_code == "version_conflict"
    assert store.writes == 0


def test_background_queue_records_missing_session_and_store_failure():
    missing = ContextMaintenanceQueue(_SnapshotStore(None))
    missing.submit(tenant_id="tenant-a", session_id="missing")
    assert missing.run_pending()[0].error_code == "session_not_found"

    class _BrokenStore(_SnapshotStore):
        def load(self, *, tenant_id, session_id):
            raise TimeoutError("checkpoint unavailable")

    broken = ContextMaintenanceQueue(_BrokenStore(None))
    broken.submit(tenant_id="tenant-a", session_id="session-a")
    task = broken.run_pending()[0]
    assert task.status == "failed"
    assert task.error_code == "snapshot_store_error"
