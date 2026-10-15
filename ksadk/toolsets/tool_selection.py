"""Pure toolset selection and registry-text search ranking.

Registration, tool schemas, dispatch and argument normalization stay in the
public ``ksadk.toolsets`` facade.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


def _canonical_toolset_group(name: object) -> str:
    value = str(name).strip().lower()
    if value == "skills":
        return "skill"
    return value


def _normalize_include(include: Iterable[str] | str | None) -> list[str]:
    if include is None:
        return []
    if isinstance(include, str):
        return [include]
    return [str(item) for item in include]


def _normalize_tool_name(tool_name: str | None) -> str:
    return str(tool_name or "").strip()


def _enabled_backend(backend: str) -> bool:
    return backend not in {"", "disabled", "none", "off"}


def _normalize_mode(mode: str) -> str:
    normalized = str(mode or "direct").strip().lower()
    if normalized in {"off", "none", "disabled"}:
        return "off"
    if normalized in {"dispatcher", "dispatch"}:
        return "dispatcher"
    if normalized in {"deferred", "search"}:
        return "deferred"
    return "direct"


def _search_terms(text: str) -> list[str]:
    terms = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    synonyms = {
        "edit": ["edit", "replace", "modify", "patch", "write", "file"],
        "file": ["file", "workspace", "read", "list", "search", "edit"],
        "grep": ["grep", "search", "rg", "find"],
        "run": ["run", "command", "shell", "code", "execute"],
        "web": ["web", "fetch", "search", "url", "http"],
        "读": ["read", "file", "workspace"],
        "改": ["edit", "replace", "file"],
        "搜": ["search", "grep", "find"],
    }
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        expanded.extend(synonyms.get(term, []))
    return expanded


def _tool_search_text(spec: Mapping[str, Any]) -> str:
    values = [
        str(spec.get("name") or ""),
        str(spec.get("group") or ""),
        str(spec.get("description") or ""),
        " ".join((spec.get("args") or {}).keys()) if isinstance(spec.get("args"), Mapping) else "",
    ]
    return " ".join(values).lower()


def _tool_search_score(terms: list[str], haystack: str, spec: Mapping[str, Any]) -> float:
    score = 0.0
    name = str(spec.get("name") or "").lower()
    for term in terms:
        if not term:
            continue
        if term in name:
            score += 4.0
        elif term in haystack:
            score += 1.0
    if str(spec.get("group") or "") == "workspace":
        score += 0.5
    return score


def _tool_search_reason(terms: list[str], haystack: str) -> str:
    matched = [term for term in terms if term and term in haystack]
    return f"matched: {', '.join(matched[:4])}" if matched else "matched registry metadata"
