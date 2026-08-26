from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".zip",
    ".whl",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
}
SKIP_DIRECTORY_NAMES = {
    ".cache",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
    "dist-alias",
    "node_modules",
}
PATTERN = re.compile(
    r"pypi-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC|DSA) "
    r"PRIVATE KEY|SecretAccessKey\s*[:=]\s*[^<\s]+"
)
PUBLIC_DOC_PATTERN = re.compile(
    r"\b(?:aicp\.(?:inner|internal)\.api|iam\.inner\.api)\.ksyun\.com\b"
    r"|\b0611agent-xiayu\b|\bezone\b",
    re.IGNORECASE,
)
PUBLIC_DOC_PREFIXES = ("README", "CHANGELOG.md", "docs/", "docs-site/")


def _source_files() -> list[str]:
    """Return tracked files, or clean-export files when no Git metadata exists."""
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if tracked.returncode == 0:
        return tracked.stdout.splitlines()

    return [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(
            part in SKIP_DIRECTORY_NAMES for part in path.relative_to(ROOT).parts
        )
    ]


def _is_public_doc_path(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    return any(
        normalized == prefix
        or normalized.startswith(prefix)
        or f"/{prefix}" in normalized
        for prefix in PUBLIC_DOC_PREFIXES
    )


def audit_paths(root: Path, relative_paths: list[str]) -> list[str]:
    hits: list[str] = []
    for relative in relative_paths:
        path = root / relative
        if not path.is_file():
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or path.name.endswith(".egg-info"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if PATTERN.search(line) or (
                _is_public_doc_path(relative) and PUBLIC_DOC_PATTERN.search(line)
            ):
                hits.append(f"{path.relative_to(root)}:{lineno}:{line}")
    return hits


def main() -> int:
    hits = audit_paths(ROOT, _source_files())
    if hits:
        for hit in hits:
            print(hit)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
