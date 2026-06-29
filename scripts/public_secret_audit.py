from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".zip", ".whl", ".png", ".jpg", ".jpeg", ".gif"}
PATTERN = re.compile(
    r"pypi-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|SecretAccessKey\s*[:=]\s*[^<\s]+"
)


def main() -> int:
    hits: list[str] = []
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for relative in tracked:
        path = ROOT / relative
        if not path.is_file():
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or path.name.endswith(".egg-info"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}:{line}")
    if hits:
        for hit in hits:
            print(hit)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
