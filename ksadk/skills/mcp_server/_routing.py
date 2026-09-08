"""Shared Skill Center routing instructions for manifests and native hosts."""

from __future__ import annotations


def skill_routing_instructions(tool_prefix: str = "") -> str:
    return f"""Match the task to a Skill Center skill, then call
{tool_prefix}load_skill(skill_name=...) to read its full SKILL.md instructions.

For an instruction-first skill, follow the loaded instructions with your outer
agent tools. Use root_dir to locate its scripts and reference files. A scripts
directory alone does not make a skill an executable sandbox workflow.

Use {tool_prefix}execute_skills(workflow_prompt=..., skill_names=[...]) only
when the skill provides a supported isolated workflow entrypoint and the task
requires that workflow. If it returns instructions, follow them with your outer
agent tools. If it returns skipped, load the skill and check its supported usage.

Isolated execution remains subject to the configured Tool Gateway approval.
If approval_required is returned, report the pending approval and use the host's
approval flow when available. Repeating the same call does not grant approval;
do not bypass the approval policy.

Use {tool_prefix}list_skills to discover skills and
{tool_prefix}preview_skill for metadata without downloading the package.
"""
