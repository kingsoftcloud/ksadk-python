"""FastMCP stdio server that bridges ksadk skill tools into MCP-compatible runtimes.

This module exposes two MCP tools:

- ``list_skills`` -- list or search skills from configured Skill Spaces;
  accepts an optional ``query`` for keyword search and ``space_id`` for
  scoping.  Uses a TTL cache to avoid repeated Skill Service round-trips.
- ``execute_skills`` -- execute a workflow through the Skill Runtime (E2B sandbox).

When Hermes or OpenClaw container entrypoints detect ``SKILL_SPACE_ID`` in the
environment, they start this server as a stdio subprocess and register it as
an MCP server via mcporter / hermes-mcp / openclaw config, making the tools
available to the LLM agent.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Any, Callable

logger = logging.getLogger("ksadk.skills.mcp_server")

# ---------------------------------------------------------------------------
# Refresh callback registry (P2 #5: decouple from runtime-specific injectors)
# ---------------------------------------------------------------------------
_refresh_callbacks: list[Callable[[str, list[Any]], None]] = []


def register_refresh_callback(callback: Callable[[str, list[Any]], None]) -> None:
    """Register a callback invoked when the skill manifest is refreshed.

    Callbacks receive ``(instruction_text, items)`` where *instruction_text*
    is the aggregated SKILL.md text and *items* is the list of manifest items.
    Runtime-specific injectors (Hermes, OpenClaw, etc.) register themselves
    here instead of being hard-wired into the server.
    """
    _refresh_callbacks.append(callback)


def _import_mcp_server_class():
    """Import the MCP server class, compatible with both old and new MCP SDK."""
    try:
        from mcp.server.fastmcp import FastMCP
        return FastMCP
    except ImportError:
        from mcp.server.mcpserver.server import MCPServer
        return MCPServer


def _create_mcp_server():
    """Create and return a FastMCP server with skill tools registered."""
    MCPServerClass = _import_mcp_server_class()

    from ksadk.skills.manifest_cache import get_manifest_cache
    from ksadk.toolsets.skills import execute_skills as _execute_skills_impl

    mcp = MCPServerClass("ksadk-skill-center")

    @mcp.tool()
    def list_skills(
        query: str | None = None,
        space_id: str | None = None,
        max_results: int = 30,
    ) -> str:
        """List or search skills from configured Skill Spaces.

        Args:
            query: Optional keyword to search skill names and descriptions.
            space_id: Optional Skill Space ID to limit listing to one space.
            max_results: Maximum number of results (default 30).

        Returns a JSON string with an ok field and a skills array.
        """
        cache = get_manifest_cache()
        try:
            if query:
                items = cache.search(query, max_results=max(max_results, 1), space_id=space_id)
            elif space_id:
                items = cache.get_space(space_id)
            else:
                items = cache.get_all()
            items = items[:max_results]
            return json.dumps(
                {"ok": True, "skills": [item.to_dict() for item in items], "count": len(items)},
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc), "skills": []},
                ensure_ascii=False,
            )

    @mcp.tool()
    def execute_skills(
        workflow_prompt: str,
        skill_names: list[str] | None = None,
    ) -> str:
        """Execute a workflow through the configured Skill Runtime (E2B sandbox).

        Args:
            workflow_prompt: The natural-language workflow to execute.
            skill_names: Optional list of skill names to scope execution to.
                Use ``list_skills`` to discover available skill names.

        Returns a JSON string with execution results, stdout/stderr, and exit_code.
        """
        result = _execute_skills_impl(workflow_prompt=workflow_prompt, skill_names=skill_names)
        return json.dumps(result, ensure_ascii=False)

    return mcp


def _start_background_skill_refresh() -> None:
    """Start a daemon thread that periodically refreshes skill entries.

    Calls all registered refresh callbacks so runtime-specific injectors
    (Hermes skill hub, OpenClaw skill hub, etc.) stay in sync with the
    Skill Center manifest without the server hard-wiring any runtime type.
    """
    try:
        from ksadk.skills.manifest_cache import get_manifest_cache, _ttl_seconds
    except ImportError:
        logger.warning("Cannot start background skill refresh: manifest_cache import failed")
        return
    skill_space_id = os.environ.get("SKILL_SPACE_ID", "").strip()
    if not skill_space_id:
        return

    def _refresh_loop() -> None:
        import time
        interval = max(30, _ttl_seconds())
        while True:
            try:
                time.sleep(interval)
                cache = get_manifest_cache()
                items = cache.get_all(force_refresh=True)
                if not items:
                    continue
                instruction_text = cache.build_instruction_text()
                for callback in _refresh_callbacks:
                    try:
                        callback(instruction_text, items)
                    except Exception as exc:
                        logger.warning("Skill refresh callback failed: %s", exc)
                logger.info("Background skill refresh: %d skills synced", len(items))
            except Exception as exc:
                logger.warning("Background skill refresh failed: %s", exc)

    t = threading.Thread(target=_refresh_loop, daemon=True, name="skill-refresh")
    t.start()
    logger.info("Background skill refresh thread started (interval=%ss)", _ttl_seconds())


def main() -> None:
    """Run the Skill Center MCP server on stdio transport."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger.info("Starting ksadk Skill Center MCP server (stdio)")
    # Register runtime-specific refresh callbacks before starting the refresh thread.
    try:
        from ksadk.skills.mcp_server.register import _register_refresh_callbacks
        _register_refresh_callbacks()
    except ImportError:
        logger.debug("No runtime-specific refresh callbacks registered")
    _start_background_skill_refresh()
    mcp = _create_mcp_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()