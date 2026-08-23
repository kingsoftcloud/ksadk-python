"""Unit locks for the managed PostgreSQL preproduction matrix runner."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.run_phase1_managed_pg_matrix import (
    KubeDriver,
    MatrixFailure,
    run_http_matrix,
)


class _FakeCanaryClient:
    def __init__(self) -> None:
        self.enabled = True
        self.receipts: dict[tuple[str, str], dict[str, Any]] = {}
        self.accepted: dict[str, list[dict[str, Any]]] = {}

    def contract(self) -> dict[str, Any]:
        return {"digest": "d" * 64, "instance": "phase1-managed-pg"}

    def worker(self, enabled: bool) -> None:
        self.enabled = enabled

    def submit(
        self,
        session_id: str,
        *,
        command_id: str,
        idempotency_key: str,
        text: str,
    ) -> dict[str, Any]:
        key = (session_id, idempotency_key)
        if key in self.receipts:
            first = self.receipts[key]
            return {**first, "status": "duplicate"}
        rows = self.accepted.setdefault(session_id, [])
        if "-fifo-" in session_id and len(rows) >= 100:
            return {
                "status": "queue_full",
                "command_id": command_id,
                "error": {"code": "queue_full", "retryable": True},
            }
        receipt = {
            "status": "accepted",
            "command_id": command_id,
            "message_id": f"message-{len(rows) + 1}",
            "accepted_seq": len(rows) + 1,
        }
        rows.append(receipt)
        self.receipts[key] = receipt
        return receipt

    def snapshot(self, session_id: str) -> dict[str, Any]:
        rows = self.accepted.get(session_id, [])
        status = "completed" if self.enabled else "accepted"
        inbox = [
            {
                "message_id": row["message_id"],
                "accepted_seq": row["accepted_seq"],
                "status": status,
                "claimed_fence": 1 if self.enabled else None,
            }
            for row in rows
        ]
        events: list[dict[str, Any]] = [
            {
                "event_id": f"accepted-{index}",
                "seq": index,
                "event_type": "control.command_accepted",
                "payload": {"message_id": row["message_id"]},
            }
            for index, row in enumerate(rows, 1)
        ]
        if self.enabled:
            offset = len(events)
            events.extend(
                {
                    "event_id": f"claimed-{index}",
                    "seq": offset + index,
                    "event_type": "control.message_claimed",
                    "payload": {"message_id": row["message_id"]},
                }
                for index, row in enumerate(rows, 1)
            )
            offset = len(events)
            events.extend(
                {
                    "event_id": f"completed-{index}",
                    "seq": offset + index,
                    "event_type": "control.message_completed",
                    "payload": {"message_id": row["message_id"]},
                }
                for index, row in enumerate(rows, 1)
            )
        return {"inbox": inbox, "events": events}

    def wait_snapshot(self, session_id, predicate, *, timeout=180.0):
        snapshot = self.snapshot(session_id)
        assert predicate(snapshot)
        return snapshot

    def read_sse(self, session_id: str, *, after_seq: int, stop_seq: int | None = None):
        if after_seq == 0:
            return [{"event_id": "accepted-1", "seq": 1}]
        assert stop_seq is not None
        return [
            {"event_id": f"event-{seq}", "seq": seq} for seq in range(after_seq + 1, stop_seq + 1)
        ]

    def stale_fence(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "command_id": "run-stale",
            "event_id": "event-stale",
            "old_fencing_token": 1,
            "new_fencing_token": 2,
            "stale_writer_rejected": True,
        }


def test_http_matrix_produces_all_store_behavior_checks() -> None:
    checks = run_http_matrix(_FakeCanaryClient(), expected_digest="d" * 64)  # type: ignore[arg-type]
    assert set(checks) == {
        "contract_digest",
        "fifo",
        "idempotency",
        "queue_full",
        "reconnect",
        "stale_fence",
    }
    assert all(check["status"] == "pass" for check in checks.values())


def test_http_matrix_preserves_completed_checks_on_late_failure() -> None:
    class _FailsAtFence(_FakeCanaryClient):
        def stale_fence(self, session_id: str) -> dict[str, Any]:
            raise MatrixFailure("synthetic late failure")

    checks: dict[str, dict[str, Any]] = {}
    with pytest.raises(MatrixFailure, match="synthetic late failure"):
        run_http_matrix(
            _FailsAtFence(),  # type: ignore[arg-type]
            expected_digest="d" * 64,
            checks=checks,
        )
    assert set(checks) == {
        "contract_digest",
        "fifo",
        "idempotency",
        "queue_full",
        "reconnect",
    }


def test_rollbacks_must_use_an_immutable_image() -> None:
    driver = KubeDriver(
        kubeconfig="/does/not/matter",
        namespace="phase1",
        deployment="canary",
    )
    with pytest.raises(MatrixFailure, match="not digest pinned"):
        driver.set_image("registry.example/canary:mutable")


def test_pod_replacement_waits_for_ready_before_port_forward(monkeypatch) -> None:
    driver = KubeDriver(
        kubeconfig="/does/not/matter",
        namespace="phase1",
        deployment="canary",
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, timeout: float = 180.0) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(driver, "current_pod", lambda: ("new-pod", "new-uid"))
    name, uid, _ = driver.delete_pod_and_wait_replacement(
        "old-pod", "old-uid", timeout=30.0
    )
    assert (name, uid) == ("new-pod", "new-uid")
    assert any(
        "wait" in call and "--for=condition=Ready" in call and "pod/new-pod" in call
        for call in calls
    )
