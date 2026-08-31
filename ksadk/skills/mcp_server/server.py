"""FastMCP stdio server that bridges ksadk skill tools into MCP-compatible runtimes.

This module exposes the following MCP tools:

- ``list_skills`` — list skills from configured Skill Spaces
- ``search_skills`` — search skills by keyword
- ``load_skill`` — download + load a skill SKILL.md instructions
- ``execute_skills`` — execute a workflow through the Skill Runtime (E2B sandbox)

When Hermes or OpenClaw container entrypoints detect ``SKILL_SPACE_ID`` in the
environment, they start this server as a stdio subprocess and register it as
an MCP server via mcporter / hermes-mcp config, making the tools available to
the LLM agent.
"""

from __future__ import annotations

import json
import logging
import sys

logger = logging.getLogger("ksadk.skills.mcp_server")


def _create_mcp_server():
    """Create and return a FastMCP server with skill tools registered."""
    from mcp.server.fastmcp import FastMCP

    from ksadk.toolsets.skills import (
        execute_skills,
        list_skill_spaces,
        list_skills,
        load_skill,
        search_skills,
    )

    mcp = FastMCP("ksadk-skill-center")

    @mcp.tool()
    def mcp_list_skill_spaces() -> str:
        """List visible Skill Spaces with id/name/description.

        Returns a JSON string with an ok field and a spaces array.
        Each space has space_id, space_name, description, and configured flag.
        """
        result = list_skill_spaces()
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool()
    def mcp_list_skills(space_id: str | None = None) -> str:
        """List skills discoverable from configured Skill Spaces.

        Args:
            space_id: Optional Skill Space ID to limit listing to one space.

        Returns a JSON string with an ok field and a skills array.
        """
        result = list_skills(space_id=space_id)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool()
    def mcp_search_skills(query: str, max_results: int = 10, space_id: str | None = None) -> str:
        """Search skills by keyword across configured Skill Spaces.

        Args:
            query: Search keyword or phrase.
            max_results: Maximum number of results to return (default 10).
            space_id: Optional Skill Space ID to limit search to one space.

        Returns a JSON string with an ok field and a results array.
        """
        result = search_skills(query=query, max_results=max_results, space_id=space_id)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool()
    def mcp_load_skill(skill_name: str, space_id: str | None = None) -> str:
        """Download and load a skill SKILL.md instructions from configured Skill Spaces.

        Args:
            skill_name: The skill name to load (case-insensitive match).
            space_id: Optional Skill Space ID for precise targeting.

        Returns a JSON string with skill instructions and metadata.
        """
        result = load_skill(skill_name=skill_name, space_id=space_id)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool()
    def mcp_execute_skills(workflow_prompt: str, skill_names: list[str] | None = None) -> str:
        """Execute a workflow through the configured Skill Runtime (E2B sandbox).

        Args:
            workflow_prompt: The natural-language workflow to execute.
            skill_names: Optional list of skill names to scope execution to.

        Returns a JSON string with execution results, stdout/stderr, and exit_code.
        """
        result = execute_skills(
            workflow_prompt=workflow_prompt,
            skill_names=skill_names,
        )
        return json.dumps(result, ensure_ascii=False)

    return mcp


def main() -> None:
    """Run the Skill Center MCP server on stdio transport."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger.info("Starting ksadk Skill Center MCP server (stdio)")
    mcp = _create_mcp_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
