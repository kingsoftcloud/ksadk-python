"""Cursor-driven projections of real Provider child execution facts."""

from __future__ import annotations

import asyncio

from .domain import member_key
from .store import digest

_STATUSES = {
    "run.started": "running",
    "run.completed": "succeeded",
    "run.failed": "failed",
    "run.canceled": "cancelled",
    "run.interrupted": "interrupted",
}


async def project_sources(runtime, domain) -> None:
    if not callable(getattr(runtime.host, "source_events", None)):
        return
    with domain.store.transaction() as tx:
        deliveries = tx.list("delivery")
    for delivery in deliveries:
        if not delivery.get("runId") or delivery.get("_sourceProjectionDone"):
            continue
        with domain.store.transaction() as tx:
            group = tx.get("group", delivery["groupId"])
            member = tx.get("member", member_key(group["groupId"], delivery["memberId"]))
            if member["sessionId"] != delivery["_sessionId"]:
                previous = next(
                    (
                        old
                        for old in member.get("_bindingHistory", [])
                        if old["sessionId"] == delivery["_sessionId"]
                    ),
                    None,
                )
                if previous:
                    member = {**member, **previous}
        try:
            batch = await runtime.host.source_events(
                runtime._scope(group, member), after=delivery.get("_sourceCursor", 0), limit=500
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        with domain.store.transaction() as tx:
            for event in batch["items"]:
                if event.get("run_id") != delivery["runId"] or event.get("family") != "runtime":
                    continue
                payload = event.get("payload") or {}
                source = payload.get("source") or {}
                metadata = source.get("metadata") or {}
                child_id = source.get("native_run_id")
                parent_id = metadata.get("parent_run_id")
                status = _STATUSES.get(metadata.get("native_event_type"))
                if not child_id or not parent_id or not status or child_id == delivery["runId"]:
                    continue
                key = "child_" + digest([delivery["deliveryId"], child_id])[7:]
                previous = tx.get("child_invocation", key, required=False)
                if previous and previous["status"] in {"succeeded", "failed", "cancelled"}:
                    continue
                record = {
                    "nodeId": key,
                    "groupId": group["groupId"],
                    "teamRunId": delivery["teamRunId"],
                    "kind": "child_invocation",
                    "title": str(metadata.get("agent_id") or "SubAgent"),
                    "status": status,
                    "memberId": member["memberId"],
                    "source": {
                        "authorityRef": runtime.authority_ref,
                        "groupId": group["groupId"],
                        "memberId": member["memberId"],
                        "bindingRef": member["bindingRef"],
                        "providerRef": member["binding"]["providerRef"],
                        "sessionId": member["sessionId"],
                        "runId": delivery["runId"],
                        **({"itemId": payload["item_id"]} if payload.get("item_id") else {}),
                    },
                    "_nativeRunId": child_id,
                    "_parentRunId": parent_id,
                    "_sourceEventId": event["event_id"],
                    "_sourceSeq": event["seq"],
                }
                if delivery.get("_taskId"):
                    record["taskId"] = delivery["_taskId"]
                    record["attemptId"] = delivery["_attemptId"]
                domain.publish(tx, "child_invocation", key, record)
            current = tx.get("delivery", delivery["deliveryId"])
            current["_sourceCursor"] = batch["cursor"]
            if current.get("_terminalState") and not batch["items"]:
                current["_sourceProjectionDone"] = True
            tx.put("delivery", current["deliveryId"], current)
