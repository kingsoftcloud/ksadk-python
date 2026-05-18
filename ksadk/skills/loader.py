from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocalSkill:
    name: str
    description: str
    root_dir: Path
    skill_md: Path
    body: str


def load_local_skill(root_dir: str | Path) -> LocalSkill:
    root = Path(root_dir)
    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md not found: {skill_md}")
    content = skill_md.read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(content)
    return LocalSkill(
        name=metadata.get("name") or root.name,
        description=metadata.get("description") or "",
        root_dir=root,
        skill_md=skill_md,
        body=body,
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
