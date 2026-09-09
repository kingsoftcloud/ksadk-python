"""Private stdio MCP adapter for frozen managed-runtime platform resources."""

from __future__ import annotations

import os
import time
from dataclasses import asdict
from uuid import uuid4

from mcp.server.fastmcp import FastMCP


def _enabled(kind: str) -> bool:
    return kind in {
        item.strip()
        for item in os.environ.get("KSADK_PLATFORM_RESOURCE_KINDS", "").split(",")
        if item.strip()
    }


def _subject() -> str:
    value = os.environ.get("KSADK_PLATFORM_RESOURCE_SUBJECT", "").strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("trusted platform-resource subject is unavailable")
    return value


def create_server() -> FastMCP:
    server = FastMCP("KsADK Platform Resources")

    if _enabled("memory-instance"):
        from ksadk.memory.service import LongTermMemoryService, format_memory_entries

        @server.tool()
        def load_memory(query: str, top_k: int = 5) -> dict:
            """Search the Agent's bound long-term memory for relevant user facts."""

            service = LongTermMemoryService.from_env()
            entries = service.search_entries(user_id=_subject(), query=query, top_k=top_k)
            if service.last_error:
                return {"ok": False, "error": service.last_error, "items": []}
            return {
                "ok": True,
                "items": entries,
                "formatted_text": format_memory_entries(entries),
            }

        if os.environ.get("KSADK_PLATFORM_RESOURCE_MEMORY_WRITE", "").lower() == "true":

            @server.tool()
            def save_memory(content: str) -> dict:
                """Persist one explicit, self-contained user fact in bound long-term memory."""

                service = LongTermMemoryService.from_env()
                session_id = "mcp-" + uuid4().hex
                ok = service.save_text(
                    user_id=_subject(),
                    content=content,
                    metadata={
                        "agent_id": os.environ.get("KSADK_PLATFORM_RESOURCE_AGENT_ID", ""),
                        "session_id": session_id,
                        "runner_type": "managed-runtime-mcp",
                        "flush": True,
                    },
                    session_id=session_id,
                    flush=True,
                )
                if not ok:
                    return {
                        "ok": False,
                        "status": "failed",
                        "error": service.last_error or "memory write was not acknowledged",
                    }
                status = None
                for attempt in range(6):
                    status = service.get_extraction_status(
                        user_id=_subject(),
                        session_id=session_id,
                        confirm_searchable=True,
                        expected_content=content,
                    )
                    if status.status in {"extracted", "duplicate_skipped", "failed"}:
                        break
                    if attempt < 5:
                        time.sleep(1)
                return {
                    "ok": status is not None and status.status != "failed",
                    "status": status.status if status is not None else "accepted_not_extracted",
                    "searchable": bool(status and status.searchable),
                    "session_id": session_id,
                }

            @server.tool()
            def memory_status(session_id: str, expected_content: str = "") -> dict:
                """Check extraction and search visibility for a prior explicit memory write."""

                service = LongTermMemoryService.from_env()
                status = service.get_extraction_status(
                    user_id=_subject(),
                    session_id=session_id,
                    confirm_searchable=bool(expected_content.strip()),
                    expected_content=expected_content,
                )
                return {"ok": not status.error_code, **asdict(status)}

    if _enabled("knowledge-base"):
        from ksadk.knowledge_base.service import KnowledgeBaseService

        @server.tool()
        def search_knowledge_base(query: str, top_k: int = 5) -> dict:
            """Search the Agent's bound knowledge base."""

            service = KnowledgeBaseService.from_env()
            results = service.search(query, top_k)
            if service.last_error:
                return {"ok": False, "error": service.last_error, "items": []}
            return {"ok": True, "items": [item.model_dump() for item in results]}

    if _enabled("skill-space"):
        from ksadk.toolsets.skills import list_skill_spaces, list_skills, load_skill, search_skills

        server.tool()(list_skill_spaces)
        server.tool()(list_skills)
        server.tool()(search_skills)
        server.tool()(load_skill)

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()


__all__ = ["create_server", "main"]
