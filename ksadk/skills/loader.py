from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LocalSkill:
    name: str
    description: str
    root_dir: Path
    skill_md: Path
    body: str
    # agentskills.io 标准对齐（G4）：宽容解析，未知键进 metadata 不报错。
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] | None = None
    warnings: tuple[str, ...] = ()


def load_local_skill(root_dir: str | Path) -> LocalSkill:
    root = Path(root_dir)
    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md not found: {skill_md}")
    content = skill_md.read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(content)
    warnings: list[str] = []
    name = metadata.get("name") or root.name
    if metadata.get("name") and metadata["name"] != root.name:
        warnings.append(
            f"skill name {metadata['name']!r} 与目录名 {root.name!r} 不一致，以 frontmatter 为准"
        )
    allowed_tools: tuple[str, ...] | None = None
    raw_allowed = metadata.get("allowed-tools")
    if raw_allowed:
        items = tuple(item.strip() for item in raw_allowed.split(",") if item.strip())
        if items:
            allowed_tools = items
    return LocalSkill(
        name=name,
        description=metadata.get("description") or "",
        root_dir=root,
        skill_md=skill_md,
        body=body,
        metadata=dict(metadata),
        allowed_tools=allowed_tools,
        warnings=tuple(warnings),
    )


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content
    raw_meta = content[4:end]
    body = content[end + 5 :]
    metadata: dict[str, str] = {}
    for line in raw_meta.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, body


_SKIP_DIR_NAMES = frozenset({"node_modules", ".git", ".venv", "__pycache__"})


def discover_local_skills(root: str | Path) -> list[LocalSkill]:
    """递归发现 root 下的本地 Skill（agentskills.io 目录形态）。

    标准允许嵌套目录；跳过依赖/版本控制/隐藏目录。结果按路径稳定排序。
    """
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"skill root not found: {root_path}")
    skills: list[LocalSkill] = []
    for skill_md in sorted(root_path.rglob("SKILL.md")):
        relative_parts = skill_md.parent.relative_to(root_path).parts
        if any(
            part in _SKIP_DIR_NAMES or part.startswith(".")
            for part in relative_parts
        ):
            continue
        skills.append(load_local_skill(skill_md.parent))
    return skills
