#!/usr/bin/env python3
"""Write deterministic source provenance for the next wheel and sdist build.

The generated file is intentionally ignored by Git.  It is package payload,
not source: every build overwrites it from the current checkout, and the
release preflight rejects dirty or stale provenance in either archive.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Sequence

from ksadk.version import VERSION

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "ksadk" / "_build_provenance.json"
EXPORT_MANIFEST = "export-manifest.json"
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _clean_export_provenance(root: Path) -> tuple[str, str]:
    """Read the identity recorded while preparing a Git-free public export."""

    manifest_path = root / EXPORT_MANIFEST
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "Git metadata is unavailable and this directory has no valid clean export manifest"
        ) from error
    commit = str(payload.get("sourceCommit") or "").lower()
    source_tree = str(payload.get("sourceTree") or "")
    if not _COMMIT_PATTERN.fullmatch(commit) or source_tree != "clean":
        raise RuntimeError(
            "clean export manifest must carry a clean 40-character sourceCommit"
        )
    return commit, source_tree


def build_provenance(root: Path = ROOT) -> dict[str, object]:
    try:
        commit = _git(root, "rev-parse", "HEAD")
        status = _git(root, "status", "--porcelain", "--untracked-files=all")
        source_tree = "dirty" if status else "clean"
    except subprocess.CalledProcessError:
        commit, source_tree = _clean_export_provenance(root)
    return {
        "schemaVersion": 1,
        "version": VERSION,
        "sourceCommit": commit,
        "sourceTree": source_tree,
    }


def write_build_provenance(
    output: Path = DEFAULT_OUTPUT,
    *,
    root: Path = ROOT,
) -> dict[str, object]:
    payload = build_provenance(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return payload


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args([] if argv is None else argv)
    payload = write_build_provenance(args.output.resolve())
    print(
        "Prepared KsADK build provenance: "
        f"version={payload['version']}, commit={payload['sourceCommit']}, "
        f"tree={payload['sourceTree']}"
    )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
