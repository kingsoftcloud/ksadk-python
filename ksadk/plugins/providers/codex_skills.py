"""Resolve pinned Codex bundle Skill inputs without escaping the bundle root."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ksadk.plugins.host import PluginHostError


def _resolve_skills(root: Path, value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PluginHostError("codex_skill_invalid", "Bundle Skills must be a list")
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PluginHostError(
                "codex_skill_invalid", f"Bundle Skills[{index}] must be an object"
            )
        name = str(item.get("name") or "").strip()
        relative = PurePosixPath(str(item.get("bundlePath") or item.get("bundle_path") or ""))
        if not name or not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise PluginHostError(
                "codex_skill_invalid", f"Bundle Skills[{index}] has an invalid native path"
            )
        path = (root / Path(*relative.parts)).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise PluginHostError(
                "codex_skill_invalid", f"Bundle Skill {name!r} escapes the Bundle root"
            ) from error
        if not (path / "SKILL.md").is_file():
            raise PluginHostError("codex_skill_missing", f"Bundle Skill {name!r} has no SKILL.md")
        if name in seen:
            raise PluginHostError("codex_skill_invalid", f"duplicate Bundle Skill {name!r}")
        seen.add(name)
        # Codex's native SkillInput expects the concrete SKILL.md source, not
        # its containing directory.  A directory is accepted by the Python
        # model but silently omitted by the real App Server turn projection.
        resolved.append({"name": name, "path": str(path / "SKILL.md")})
    return resolved
