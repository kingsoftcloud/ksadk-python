from __future__ import annotations

from pathlib import Path

TEXT_ARTIFACT_SUFFIXES = frozenset(
    {
        ".csv",
        ".json",
        ".jsonl",
        ".log",
        ".md",
        ".rst",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


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
    return [str(path) for path in sorted(output_dir.rglob("*")) if path.is_file()]


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


def collect_text_output(
    artifacts: list[str],
    *,
    allowed_root: Path,
    max_bytes: int,
) -> tuple[str, bool]:
    """Read bounded text artifacts without escaping the Skill workdir.

    Artifact paths are controlled by the executed Skill, so only regular,
    non-symlink text files contained by ``allowed_root`` are eligible. Binary
    artifacts remain available through artifact metadata and are never placed
    in the model-facing result.
    """

    root = allowed_root.resolve()
    candidates: list[Path] = []
    seen: set[Path] = set()
    for raw_path in artifacts:
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        try:
            if path.is_symlink():
                continue
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if (
            resolved in seen
            or not resolved.is_file()
            or resolved.suffix.lower() not in TEXT_ARTIFACT_SUFFIXES
        ):
            continue
        seen.add(resolved)
        candidates.append(resolved)

    budget = max(0, max_bytes)
    if not candidates or budget == 0:
        return "", bool(candidates)

    chunks: list[str] = []
    truncated = False
    include_headers = len(candidates) > 1
    for index, path in enumerate(candidates):
        separator = "\n\n" if index else ""
        header = f"--- {path.name} ---\n" if include_headers else ""
        prefix = separator + header
        prefix_bytes = prefix.encode("utf-8")
        if len(prefix_bytes) >= budget:
            truncated = True
            break

        try:
            with path.open("rb") as stream:
                payload = stream.read(budget - len(prefix_bytes) + 1)
        except OSError:
            continue
        budget -= len(prefix_bytes)
        if len(payload) > budget:
            payload = payload[:budget]
            truncated = True
        chunks.append(prefix + payload.decode("utf-8", errors="replace"))
        budget -= len(payload)
        if truncated or budget == 0:
            truncated = truncated or index < len(candidates) - 1
            break

    return "".join(chunks), truncated
