from __future__ import annotations

import re
from typing import Iterable, List

from packaging.requirements import InvalidRequirement, Requirement


_NORMALIZED_NAME_RE = re.compile(r"[-_.]+")


def parse_requirements_text(content: str) -> List[str]:
    return [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def merge_requirement_lists(*groups: Iterable[str]) -> List[str]:
    merged: List[str] = []
    requirement_positions: dict[str, int] = {}

    for group in groups:
        for raw_requirement in group:
            requirement = raw_requirement.strip()
            if not requirement or requirement.startswith("#"):
                continue

            key = _requirement_key(requirement)
            if key is None:
                merged.append(requirement)
                continue

            existing = requirement_positions.get(key)
            if existing is None:
                requirement_positions[key] = len(merged)
                merged.append(requirement)
            else:
                merged[existing] = requirement

    return merged


def exclude_requirement_names(
    requirements: Iterable[str],
    *,
    excluded_names: Iterable[str],
) -> List[str]:
    excluded = {
        _NORMALIZED_NAME_RE.sub("-", name).lower()
        for name in excluded_names
    }
    filtered: List[str] = []

    for raw_requirement in requirements:
        requirement = raw_requirement.strip()
        if not requirement or requirement.startswith("#"):
            continue

        key = _requirement_key(requirement)
        if key is not None and key in excluded:
            continue

        filtered.append(requirement)

    return filtered


def _requirement_key(requirement: str) -> str | None:
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        return None
    return _NORMALIZED_NAME_RE.sub("-", parsed.name).lower()
