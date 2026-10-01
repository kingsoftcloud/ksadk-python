from __future__ import annotations

from typing import Mapping

from ksadk.sessions import SessionEvent

_KNOWN_TOOL_RECEIPT_STATUSES = frozenset({"completed", "failed", "succeeded"})


def _validate_tool_receipt_event(event: SessionEvent) -> str:
    """Reject malformed persisted output/status pairs before replay."""

    metadata = event.metadata or {}
    if "tool_output" not in metadata:
        raise ValueError("tool receipt is missing tool_output")
    receipt = metadata.get("tool_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("tool receipt is missing receipt metadata")
    status = str(receipt.get("status") or "").strip().lower()
    if status not in _KNOWN_TOOL_RECEIPT_STATUSES:
        raise ValueError(f"tool receipt has unknown status {status!r}")
    output = metadata["tool_output"]
    if isinstance(output, Mapping):
        expected = "failed" if output.get("ok") is False else "completed"
        normalized_status = "completed" if status == "succeeded" else status
        if normalized_status != expected:
            raise ValueError("tool receipt status does not match mapping tool_output ok value")
    return status
