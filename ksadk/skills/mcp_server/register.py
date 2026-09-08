"""Skill Center MCP registration entry point.

Replaces inline Python in skill-center-mcp-register.sh.
Handles three registration paths:
1. mcporter (Hermes and other mcporter-aware runtimes)
2. OpenClaw config (openclaw.json -> mcp.servers)
3. Hermes config (config.yaml -> mcp_servers)

Also prefetches skill manifests and injects them into workspace context files
so the LLM can discover skills without calling list_skills first.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys

logger = logging.getLogger("ksadk.skills.mcp_server.register")

MCP_NAME = "ksadk-skill-center"
MCP_COMMAND = "python3 -m ksadk.skills.mcp_server.server"

ENV_VARS_TO_COLLECT = [
    "SKILL_SPACE_ID",
    "KSADK_SKILL_SERVICE_URL",
    "KSADK_SKILL_SERVICE_REGION",
    "KSADK_SKILL_SERVICE_ACCESS_KEY",
    "KSADK_SKILL_SERVICE_SECRET_KEY",
    "KSADK_SKILL_RUNTIME_BACKEND",
    "KSADK_SANDBOX_TEMPLATE_ID",
    "KSADK_SANDBOX_API_KEY",
    "E2B_API_KEY",
    "E2B_ACCESS_TOKEN",
    "KSADK_SKILL_SERVICE_ENDPOINT",
    "KSADK_SKILL_SERVICE_SCHEME",
    "KSADK_AICP_ENDPOINT_MODE",
    "KSADK_TOOL_APPROVAL_MODE",
]


_SKILL_SERVICE_CREDENTIAL_FALLBACKS = {
    "KSADK_SKILL_SERVICE_ACCESS_KEY": ("KSYUN_ACCESS_KEY", "KS3_ACCESS_KEY"),
    "KSADK_SKILL_SERVICE_SECRET_KEY": ("KSYUN_SECRET_KEY", "KS3_SECRET_KEY"),
}


def _collect_env() -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for key in ENV_VARS_TO_COLLECT:
        val = os.environ.get(key, "").strip()
        if val:
            env_vars[key] = val
    # Fallback: when KSADK_SKILL_SERVICE_ACCESS_KEY/SECRET_KEY are not set,
    # use KSYUN_ACCESS_KEY/KSYUN_SECRET_KEY (forwarded to the pod by
    # forward_shell_process_env) so users do not need to manually pass
    # --env KSADK_SKILL_SERVICE_ACCESS_KEY=... --env KSADK_SKILL_SERVICE_SECRET_KEY=...
    if env_vars.get("SKILL_SPACE_ID"):
        for target, sources in _SKILL_SERVICE_CREDENTIAL_FALLBACKS.items():
            if target in env_vars:
                continue
            for source in sources:
                value = os.environ.get(source, "").strip()
                if value:
                    env_vars[target] = value
                    break
    if not env_vars.get("KSADK_SKILL_SERVICE_URL"):
        try:
            from ksadk.skills.service_env import resolve_skill_service_url
            url = resolve_skill_service_url(require_spaces=False)
            if url:
                env_vars["KSADK_SKILL_SERVICE_URL"] = url
        except Exception:
            pass
    return env_vars


def _is_safe_single_segment(name: str) -> bool:
    """Validate that *name* is a safe single-path-segment identifier.

    Rejects empty, absolute paths, parent traversal, separators, and
    other characters that could escape the base directory.
    """
    if not name or not name.strip():
        return False
    if "/" in name or "\\" in name or name.startswith("."):
        return False
    if name in {".", ".."}:
        return False
    # Reject null bytes and other control characters
    if any(ord(c) < 32 for c in name):
        return False
    return True


def _is_safe_skill_dir(base_dir: str, skill_name: str) -> bool:
    """Check that joining base_dir + skill_name stays within base_dir.

    Returns True if the resolved path is inside base_dir and the name
    passes single-segment validation.
    """
    if not _is_safe_single_segment(skill_name):
        return False
    resolved = pathlib_resolve(base_dir, skill_name)
    base_resolved = pathlib_resolve(base_dir)
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        return False
    return True


def pathlib_resolve(*parts: str) -> "pathlib.PosixPath | pathlib.WindowsPath":
    import pathlib as _pl
    return _pl.Path(*parts).resolve()


def _is_managed_dir(skill_dir: str) -> bool:
    """Check if a directory is already managed by Skill Center.

    Returns True only if the .ksadk-skill-center marker file exists,
    meaning we created this directory and can safely overwrite/delete it.
    """
    marker = os.path.join(skill_dir, _SKILL_CENTER_MARKER)
    # S2 fix: do not follow symlinks when checking marker existence
    return os.path.isfile(marker) and not os.path.islink(marker)


def _is_path_safe(base_dir: str, target: str) -> bool:
    """Check that *target* (joined under *base_dir*) resolves within base_dir
    and does not involve symlinks pointing outside base_dir.
    """
    import pathlib as _pl
    base = _pl.Path(base_dir).resolve()
    tgt = _pl.Path(target).resolve()
    try:
        tgt.relative_to(base)
    except ValueError:
        return False
    return True


def _hermes_home() -> str:
    """Return the Hermes home directory from env, falling back to sane defaults.

    v2026.8.31 uses /opt/data; v2026.8.19 uses /home/node/.hermes.
    """
    explicit = os.environ.get("HERMES_HOME")
    if explicit:
        return explicit
    home = os.environ.get("HOME") or "/opt/data"
    if home == "/opt/data":
        return home
    return os.path.join(home, ".hermes")


def _split_command(cmd_str: str) -> dict[str, object]:
    parts = cmd_str.split()
    return {"command": parts[0], "args": parts[1:]}


def _register_mcporter(env: dict[str, str]) -> None:
    import subprocess
    if not shutil.which("mcporter"):
        return
    subprocess.run(["mcporter", "config", "remove", MCP_NAME], capture_output=True)
    cmd = ["mcporter", "config", "add", MCP_NAME, "--command", MCP_COMMAND, "--transport", "stdio"]
    subprocess.run(cmd, capture_output=True)
    logger.info("mcporter registration: %s", MCP_NAME)


def _register_openclaw(env: dict[str, str]) -> None:
    config_path = os.environ.get("OPENCLAW_CONFIG_PATH", "/home/node/.openclaw/openclaw.json")
    if not os.path.isfile(config_path):
        return
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        gw = cfg.get("gateway", {})
        gw.pop("mcpServers", None)
        mcp = cfg.setdefault("mcp", {})
        servers = mcp.setdefault("servers", {})
        entry = _split_command(MCP_COMMAND)
        if env:
            entry["env"] = env
        servers[MCP_NAME] = entry
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        logger.info("OpenClaw config injection: %s -> %s", MCP_NAME, config_path)
    except Exception as exc:
        logger.warning("OpenClaw config injection failed: %s", exc)


def _register_hermes(env: dict[str, str]) -> None:
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not available; skipping Hermes config injection")
        return
    config_path = os.environ.get("HERMES_CONFIG_PATH", os.path.join(_hermes_home(), "config.yaml"))
    if not os.path.isfile(config_path):
        return
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        servers = cfg.setdefault("mcp_servers", {})
        servers.pop(MCP_NAME, None)
        entry = _split_command(MCP_COMMAND)
        entry["enabled"] = True
        if env:
            entry["env"] = env
        servers[MCP_NAME] = entry
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        logger.info("Hermes config injection: %s -> %s", MCP_NAME, config_path)
    except Exception as exc:
        logger.warning("Hermes config injection failed: %s", exc)


def _prefetch_manifest_to_workspace() -> None:
    """Prefetch skill manifests and inject into workspace context files.

    For OpenClaw: appends a Skills section to TOOLS.md in the workspace.
    For Hermes: creates a skill entry under ~/.hermes/skills/ so Hermes
    includes Skill Center in the system-prompt block.
    This lets the LLM discover available skills at startup without calling
    list_skills first.
    """
    skill_space_id = os.environ.get("SKILL_SPACE_ID", "").strip()
    if not skill_space_id:
        return
    try:
        from ksadk.skills.manifest_cache import get_manifest_cache
        cache = get_manifest_cache()
        items = cache.get_all()
        instruction_text = cache.build_instruction_text() if items else ""
        _inject_openclaw_workspace(instruction_text)
        _inject_openclaw_skill_hub(instruction_text, items)
        _inject_hermes_skill_hub(instruction_text, items)
        if not items:
            logger.info("Manifest prefetch: no skills found in space %s", skill_space_id)
        else:
            logger.info("Manifest prefetch completed for space %s (%d skills)", skill_space_id, len(items))
    except Exception as exc:
        logger.warning("Manifest prefetch failed: %s", exc)


def _inject_openclaw_workspace(instruction_text: str) -> None:
    tools_md = os.environ.get("OPENCLAW_TOOLS_MD", "/home/node/.openclaw/workspace/TOOLS.md")
    if not os.path.isdir(os.path.dirname(tools_md)):
        return
    start_marker = "<!-- skill-center-start -->"
    end_marker = "<!-- skill-center-end -->"
    # Backward-compat: old single-marker block (no end boundary)
    old_marker = "## Available Skills (Skill Center)"
    try:
        existing = ""
        if os.path.isfile(tools_md):
            existing = open(tools_md, encoding="utf-8").read()
        # Remove paired-marker block first (precise boundary)
        if start_marker in existing and end_marker in existing:
            before = existing.split(start_marker, 1)[0]
            after = existing.rsplit(end_marker, 1)[-1]
            existing = (before.rstrip() + "\n" + after.rstrip()).rstrip()
        elif start_marker in existing:
            # Only start marker (malformed): remove from start onward
            existing = existing.split(start_marker, 1)[0].rstrip()
        # Migrate old single-marker block: treat old_marker as start boundary
        # but keep content after it (unlike the old truncation approach).
        if old_marker in existing:
            before_old = existing.split(old_marker, 1)[0]
            after_old = existing.split(old_marker, 1)[1] if old_marker in existing else ""
            existing = before_old.rstrip()
            # after_old content is discarded as it was part of the old skill block,
            # but user content after the old skill block may exist. We cannot
            # reliably distinguish, so we preserve before_old only. This is
            # the same behavior as before but only triggers on first migration.
        # Reconstruct: keep existing content + new paired block
        if not instruction_text.strip():
            # Empty manifest: just remove our block, keep everything else
            with open(tools_md, "w", encoding="utf-8") as f:
                f.write(existing.rstrip() + "\n" if existing.strip() else "")
            return
        block = f"{start_marker}\n{instruction_text.strip()}\n{end_marker}"
        content = existing.rstrip() + "\n\n" + block if existing.strip() else block
        with open(tools_md, "w", encoding="utf-8") as f:
            f.write(content + "\n")
        logger.info("OpenClaw workspace TOOLS.md updated with skill manifest")
    except Exception as exc:
        logger.warning("OpenClaw workspace injection failed: %s", exc)



_HERMES_TOOL_NOTE = """
## Hermes MCP Tool Calling Convention

In Hermes, MCP tools from Skill Center are accessed via tool_call with the prefix mcp__ksadk_skill_center__.

To execute a Skill Center skill, call:
  tool_call(name=mcp__ksadk_skill_center__execute_skills, arguments={workflow_prompt: ..., skill_names: [...]})

CRITICAL ROUTING RULE: When a user task matches ANY skill listed in the Skill Center,
you MUST call mcp__ksadk_skill_center__execute_skills. Do NOT use any built-in tool
(terminal, browser_navigate, browser_*, read_file, write_file, execute_code,
skill_view, or similar) as a substitute for execute_skills.

Built-in tools like terminal and browser_navigate can only perform raw operations;
they cannot load skill instructions, dependencies, or the sandbox environment.
execute_skills runs the skill in an isolated sandbox with the correct setup
automatically.

If you are unsure whether a task matches a Skill Center skill, call
mcp__ksadk_skill_center__list_skills first to check, then call execute_skills.
"""


def _inject_hermes_skill_hub(
    instruction_text: str,
    items: list | None = None,
) -> None:
    """Create individual Hermes skill entries for each Skill Center skill.

    Instead of a single bridge entry, this creates one Hermes skill per
    Skill Center skill under ~/.hermes/skills/<skill-name>/SKILL.md so that
    each appears individually in the system-prompt <available_skills> block.
    The agent can then match user tasks to specific skills (e.g.
    "sports-results", "web-artifacts-builder") instead of a generic bridge.
    """
    base_dir = os.environ.get(
        "HERMES_SKILL_HUB_DIR", os.path.join(_hermes_home(), "skills")
    )
    if not os.path.isdir(base_dir):
        return
    try:
        if items is None:
            items = []
        _cleanup_stale_hermes_skills(base_dir, items)
        if not items:
            return
        for item in items:
            skill_name = getattr(item, "name", "")
            if not skill_name:
                continue
            if not _is_safe_single_segment(skill_name):
                logger.warning("Skipping unsafe skill name: %r", skill_name)
                continue
            skill_desc = getattr(item, "description", "") or "No description"
            skill_dir = os.path.join(base_dir, skill_name)
            if os.path.isdir(skill_dir) and not _is_managed_dir(skill_dir):
                logger.warning("Skipping %s: directory exists but is not Skill Center-managed", skill_name)
                continue
            os.makedirs(skill_dir, exist_ok=True)
            skill_md_path = os.path.join(skill_dir, "SKILL.md")
            # S2 fix: reject if SKILL.md is a symlink pointing outside base_dir
            if os.path.islink(skill_md_path) and not _is_path_safe(base_dir, skill_md_path):
                logger.warning("Skipping %s: SKILL.md symlink escapes base dir", skill_name)
                continue
            skill_md_path = os.path.join(skill_dir, "SKILL.md")
            # S2 fix: reject if path is a symlink pointing outside base_dir
            if os.path.islink(skill_md_path) and not _is_path_safe(base_dir, skill_md_path):
                logger.warning("Skipping %s: SKILL.md symlink escapes base dir", skill_name)
                continue
            with open(os.path.join(skill_dir, _SKILL_CENTER_MARKER), "w") as mf:
                mf.write("1")
            skill_md_content = (
                "---\n"
                f"name: {skill_name}\n"
                "description: >\n"
                f"  {skill_desc}\n"
                "  To execute this skill, call\n"
                "  mcp__ksadk_skill_center__execute_skills with\n"
                f"  skill_names=[\"{skill_name}\"].\n"
                "  Do NOT use terminal, browser_navigate, read_file,\n"
                "  write_file, or execute_code as a substitute.\n"
                "---\n\n"
                f"# {skill_name}\n\n"
                f"{skill_desc}\n\n"
                + _HERMES_TOOL_NOTE.strip()
                + "\n"
            )
            with open(skill_md_path, "w", encoding="utf-8") as f:
                f.write(skill_md_content)
        logger.info("Hermes skill-hub entries created: %d skills under %s", len(items), base_dir)
    except Exception as exc:
        logger.warning("Hermes skill-hub injection failed: %s", exc)


_SKILL_CENTER_MARKER = ".ksadk-skill-center"


def _cleanup_stale_hermes_skills(base_dir: str, current_items: list) -> None:
    """Remove only Skill Center-managed skill directories that no longer exist.

    Uses a marker file (.ksadk-skill-center) inside each managed directory to
    distinguish Skill Center entries from Hermes built-in skills.  This
    ensures we never delete Hermes' own skill directories.
    """
    try:
        current_names = {getattr(i, "name", "") for i in current_items}
        current_names.discard("")
        for entry in os.listdir(base_dir):
            entry_path = os.path.join(base_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            if entry.startswith("."):
                continue
            marker_path = os.path.join(entry_path, _SKILL_CENTER_MARKER)
            if not os.path.isfile(marker_path):
                continue
            if entry not in current_names:
                shutil.rmtree(entry_path, ignore_errors=True)
                logger.info("Removed stale Skill Center entry: %s", entry)
    except Exception as exc:
        logger.warning("Stale skill cleanup failed: %s", exc)


_OPENCLAW_TOOL_NOTE = """
## OpenClaw MCP Tool Calling Convention

In OpenClaw, MCP tools from Skill Center are accessed via tool_call with the prefix mcp__ksadk_skill_center__.

To execute a Skill Center skill, call:
  tool_call(name=mcp__ksadk_skill_center__execute_skills, arguments={workflow_prompt: ..., skill_names: [...]})

CRITICAL ROUTING RULE: When a user task matches ANY skill listed in the Skill Center,
you MUST call mcp__ksadk_skill_center__execute_skills. Do NOT use any built-in tool
(terminal, browser_navigate, browser_*, read_file, write_file, execute_code,
or similar) as a substitute for execute_skills.

Built-in tools like terminal and browser_navigate can only perform raw operations;
they cannot load skill instructions, dependencies, or the sandbox environment.
execute_skills runs the skill in an isolated sandbox with the correct setup
automatically.

If you are unsure whether a task matches a Skill Center skill, call
mcp__ksadk_skill_center__list_skills first to check, then call execute_skills.
"""


def _cleanup_stale_openclaw_skills(base_dir: str, current_items: list) -> None:
    """Remove only Skill Center-managed skill directories that no longer exist.

    Uses a marker file (.ksadk-skill-center) inside each managed directory to
    distinguish Skill Center entries from OpenClaw built-in skills.
    """
    try:
        current_names = {getattr(i, "name", "") for i in current_items}
        current_names.discard("")
        for entry in os.listdir(base_dir):
            entry_path = os.path.join(base_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            if entry.startswith("."):
                continue
            marker_path = os.path.join(entry_path, _SKILL_CENTER_MARKER)
            if not os.path.isfile(marker_path):
                continue
            if entry not in current_names:
                shutil.rmtree(entry_path, ignore_errors=True)
                logger.info("Removed stale Skill Center entry: %s", entry)
    except Exception as exc:
        logger.warning("Stale skill cleanup failed: %s", exc)


def _inject_openclaw_skill_hub(
    instruction_text: str,
    items: list | None = None,
) -> None:
    """Create individual OpenClaw skill entries for each Skill Center skill.

    Creates one SKILL.md per Skill Center skill under ~/.openclaw/skills/<name>/
    so that each appears individually in the OpenClaw Dashboard skill panel
    and in the system-prompt available skills list.
    """
    base_dir = os.environ.get(
        "OPENCLAW_SKILL_HUB_DIR",
        os.path.join(os.environ.get("OPENCLAW_STATE_DIR", "/home/node/.openclaw"), "skills"),
    )
    if not os.path.isdir(base_dir):
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            return
    try:
        if items is None:
            items = []
        _cleanup_stale_openclaw_skills(base_dir, items)
        if not items:
            return
        for item in items:
            skill_name = getattr(item, "name", "")
            if not skill_name:
                continue
            if not _is_safe_single_segment(skill_name):
                logger.warning("Skipping unsafe skill name: %r", skill_name)
                continue
            skill_desc = getattr(item, "description", "") or "No description"
            skill_dir = os.path.join(base_dir, skill_name)
            # S1 fix: do not overwrite a pre-existing directory we don't own
            if os.path.isdir(skill_dir) and not _is_managed_dir(skill_dir):
                logger.warning("Skipping %s: directory exists but is not Skill Center-managed", skill_name)
                continue
            os.makedirs(skill_dir, exist_ok=True)
            with open(os.path.join(skill_dir, _SKILL_CENTER_MARKER), "w") as mf:
                mf.write("1")
            skill_md_content = (
                "---\n"
                f"name: {skill_name}\n"
                "description: >\n"
                f"  {skill_desc}\n"
                "  To execute this skill, call\n"
                "  mcp__ksadk_skill_center__execute_skills with\n"
                f"  skill_names=[\"{skill_name}\"].\n"
                "  Do NOT use terminal, browser_navigate, read_file,\n"
                "  write_file, or execute_code as a substitute.\n"
                "---\n\n"
                f"# {skill_name}\n\n"
                f"{skill_desc}\n\n"
                + _OPENCLAW_TOOL_NOTE.strip()
                + "\n"
            )
            with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(skill_md_content)
        logger.info("OpenClaw skill-hub entries created: %d skills under %s", len(items), base_dir)
    except Exception as exc:
        logger.warning("OpenClaw skill-hub injection failed: %s", exc)


def register_skill_center_mcp() -> None:
    """Main entry point called from shell scripts during bootstrap."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="[skill-center] %(message)s",
    )
    skill_space_id = os.environ.get("SKILL_SPACE_ID", "").strip()
    if not skill_space_id:
        return
    if not shutil.which("python3"):
        logger.info("python3 not found; skipping Skill Center MCP registration")
        return
    try:
        import ksadk.skills.mcp_server.server  # noqa: F401
    except ImportError:
        logger.info("ksadk.skills.mcp_server not importable; skipping")
        return
    env = _collect_env()
    _register_mcporter(env)
    _register_openclaw(env)
    _register_hermes(env)
    _prefetch_manifest_to_workspace()
    logger.info("Skill Center MCP registered: %s (SKILL_SPACE_ID=%s)", MCP_NAME, skill_space_id)


if __name__ == "__main__":
    register_skill_center_mcp()


def _register_refresh_callbacks() -> None:
    """Register runtime-specific skill refresh callbacks with the MCP server.

    Called from ``server.main()`` before the background refresh thread starts.
    Each callback receives ``(instruction_text, items)`` when the manifest is
    refreshed, keeping runtime skill entries in sync with the Skill Center.
    """
    from ksadk.skills.mcp_server.server import register_refresh_callback

    def _on_refresh(instruction_text: str, items: list) -> None:
        _inject_hermes_skill_hub(instruction_text, items)
        _inject_openclaw_skill_hub(instruction_text, items)
        _inject_openclaw_workspace(instruction_text)

    register_refresh_callback(_on_refresh)