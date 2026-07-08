#!/usr/bin/env python3
"""Build a PyPI alias distribution for the ksadk package.

The alias package keeps the Python import/package layout as ``ksadk`` and only
changes the distribution metadata name. It is used for the compatibility PyPI
project ``agentengine-sdk-python``.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALIAS_PROJECT = "agentengine-sdk-python"
DEFAULT_ALIAS_DESCRIPTION = "Kingsoft Cloud Agent Engine SDK alias package for ksadk"

IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".worktrees",
    ".zread",
    "__pycache__",
    "build",
    "dist",
    "dist-alias",
    "htmlcov",
    "ksadk.egg-info",
    "node_modules",
    "site",
}
IGNORED_SUFFIXES = (".pyc", ".pyo")


def _ignore(_dir: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in IGNORED_DIRS or name.endswith(IGNORED_SUFFIXES):
            ignored.add(name)
    return ignored


def _rewrite_pyproject(pyproject: Path, *, alias_project: str, description: str) -> None:
    text = pyproject.read_text(encoding="utf-8")
    text, name_count = re.subn(
        r'(?m)^name = "ksadk"$',
        f'name = "{alias_project}"',
        text,
        count=1,
    )
    text, description_count = re.subn(
        r'(?m)^description = ".*"$',
        f'description = "{description}"',
        text,
        count=1,
    )
    if name_count != 1:
        raise RuntimeError("pyproject.toml 中未找到唯一的 ksadk project name")
    if description_count != 1:
        raise RuntimeError("pyproject.toml 中未找到唯一的 project description")
    pyproject.write_text(text, encoding="utf-8")


def build_alias_distribution(
    *,
    alias_project: str,
    description: str,
    out_dir: Path,
) -> None:
    out_dir = out_dir.resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ksadk-alias-src-") as tmp:
        alias_root = Path(tmp) / "src"
        shutil.copytree(ROOT, alias_root, ignore=_ignore)
        _rewrite_pyproject(
            alias_root / "pyproject.toml",
            alias_project=alias_project,
            description=description,
        )
        subprocess.run(
            ["uv", "build", "--out-dir", str(out_dir)],
            cwd=alias_root,
            check=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alias-project", default=DEFAULT_ALIAS_PROJECT)
    parser.add_argument("--description", default=DEFAULT_ALIAS_DESCRIPTION)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist-alias")
    args = parser.parse_args()

    build_alias_distribution(
        alias_project=args.alias_project,
        description=args.description,
        out_dir=args.out_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
