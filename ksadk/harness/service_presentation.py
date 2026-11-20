"""The display subset of the standalone Harness protocol."""

import re
from typing import Any

ENGINE_VIEW_CONTRACT = "agentkit.engine-view/v1"
ENGINE_EVENT_CONTRACT = "agentkit.engine-event/v1"
ENGINE_VIEW_PATH = "/_ksadk/harness/v1/views"
VIEW_HASH = re.compile(r"[a-f0-9]{64}")


def engine_view_hash(value: Any) -> str | None:
    if not isinstance(value, dict) or value.get("contract") != ENGINE_VIEW_CONTRACT:
        return None
    digest = value.get("sha256")
    return digest if isinstance(digest, str) and VIEW_HASH.fullmatch(digest) else None


def engine_presentation(view_hash: str | None, event: Any) -> dict[str, Any]:
    if (
        not isinstance(view_hash, str)
        or not VIEW_HASH.fullmatch(view_hash)
        or not isinstance(event, dict)
        or event.get("schema") != ENGINE_EVENT_CONTRACT
        or type(event.get("seq")) is not int
        or event["seq"] <= 0
        or not isinstance(event.get("kind"), str)
    ):
        return {}
    projected = {"schema": ENGINE_EVENT_CONTRACT, "seq": event["seq"]}
    for field in ("kind", "category", "title", "status", "reason", "callId"):
        if isinstance(event.get(field), str):
            projected[field] = event[field][:200]
    if event.get("kind") == "model.failure_observed" and isinstance(event.get("detail"), str):
        projected["detail"] = event["detail"][:200]
    if isinstance(event.get("tools"), list):
        projected["tools"] = [item[:128] for item in event["tools"][:32] if isinstance(item, str)]
    if isinstance(event.get("usage"), dict):
        projected["usage"] = {
            field: event["usage"][field]
            for field in ("inputTokens", "outputTokens")
            if type(event["usage"].get(field)) is int and event["usage"][field] >= 0
        }
    return {"contract": ENGINE_VIEW_CONTRACT, "sha256": view_hash, "event": projected}
