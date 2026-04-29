#!/usr/bin/env python3
"""Copy source files referenced by zread pages into a small Docker snapshot."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import unquote


ROOT = Path.cwd()
WIKI_CURRENT = ROOT / ".zread" / "wiki" / "current"
SOURCE_DIR = ROOT / ".zread" / "source"
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def current_wiki_root() -> Path:
    version = WIKI_CURRENT.read_text(encoding="utf-8").strip().removeprefix("versions/")
    return ROOT / ".zread" / "wiki" / "versions" / version


def is_local_source_href(href: str) -> bool:
    return not href.startswith(("http://", "https://", "#", "mailto:", "javascript:"))


def referenced_files(wiki_root: Path) -> list[Path]:
    files: set[Path] = set()
    for markdown in wiki_root.glob("*.md"):
        text = markdown.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            href = unquote(match.group(1).strip()).split("#", 1)[0]
            if not href or not is_local_source_href(href):
                continue
            candidate = Path(href)
            if candidate.is_absolute() or ".." in candidate.parts:
                continue
            source = ROOT / candidate
            if source.is_file():
                files.add(candidate)
    return sorted(files, key=lambda path: path.as_posix())


def main() -> int:
    wiki_root = current_wiki_root()
    files = referenced_files(wiki_root)
    if SOURCE_DIR.exists():
        shutil.rmtree(SOURCE_DIR)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    for relative in files:
        source = ROOT / relative
        target = SOURCE_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        total_bytes += source.stat().st_size

    print(f"✅ zread source snapshot: files={len(files)}, bytes={total_bytes}, dir={SOURCE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
