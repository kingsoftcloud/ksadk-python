from __future__ import annotations

from pathlib import Path


def parse_artifact_lines(stdout: str) -> list[str]:
    artifacts: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        if not line.startswith("artifact="):
            continue
        value = line.split("=", 1)[1].strip()
        if not value or value in seen:
            continue
        seen.add(value)
        artifacts.append(value)
    return artifacts


def collect_output_dir_artifacts(output_dir: Path) -> list[str]:
    if not output_dir.is_dir():
        return []
    return [
        str(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    ]


def merge_artifacts(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged
