"""Memory observability and local management routes for Studio."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query

from ksadk.memory.coordinator import agent_user_scope_id
from ksadk.memory.models import MemoryDeleteRequest, MemorySearchRequest
from ksadk.memory.providers.local_sqlite import resolve_default_memory_provider


def register_memory_routes(app: FastAPI, studio: Any) -> None:
    """Register PCM memory routes without expanding the central API module."""

    @app.get("/api/v1/runs/{run_id}/memory-events")
    async def get_run_memory_events(run_id: str):
        events = studio.event_store.events(run_id)
        items = []
        for event in events:
            data = event.data or {}
            has_memory_payload = isinstance(data, dict) and (
                "memory_event" in data
                or any(
                    isinstance(value, str) and value.startswith("memory.")
                    for value in data.values()
                )
            )
            if "memory" in str(event.type or "").lower() or has_memory_payload:
                items.append({"id": event.id, "type": event.type, "data": data})
        return {"items": items}

    @app.get("/api/v1/memories")
    async def list_memories(
        user_id: str = Query(default="local-user", alias="userId"),
        agent_id: str | None = Query(default=None, alias="agentId"),
    ):
        provider = resolve_default_memory_provider()
        scope_id = agent_user_scope_id(agent_id=agent_id, user_id=user_id) if agent_id else user_id
        result = provider.search(
            MemorySearchRequest(
                query="",
                scopes=[("user", scope_id)],
                memory_types=["profile", "fact", "episode"],
                top_k=100,
                max_tokens=10000,
                min_score=0.0,
            )
        )
        return {
            "items": [
                {
                    "memory_id": record.memory_id,
                    "scope": record.scope,
                    "scope_id": record.scope_id,
                    "memory_type": record.memory_type,
                    "content": record.content[:200],
                    "summary": record.summary,
                    "status": record.status,
                    "confidence": record.confidence,
                    "created_at": record.created_at,
                }
                for record in result.records
            ]
        }

    @app.delete("/api/v1/memories/{memory_id}")
    async def delete_memory(memory_id: str):
        provider = resolve_default_memory_provider()
        record = provider.get(memory_id)
        if record is None:
            return {"deleted": False, "status": "ok"}
        result = provider.delete(
            MemoryDeleteRequest(
                memory_id=memory_id,
                scope=record.scope,
                scope_id=record.scope_id,
                hard=True,
            )
        )
        return {"deleted": result.deleted, "status": result.status}
