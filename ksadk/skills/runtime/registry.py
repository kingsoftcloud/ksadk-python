from __future__ import annotations

import os
import re

from ksadk.skills.models import SkillRef
from ksadk.skills.runtime.base import normalize_skill_names
from ksadk.skills.service_env import (
    parse_skill_space_ids,
    public_skill_space_ids as configured_public_skill_space_ids,
    skill_space_ids as configured_skill_space_ids,
    user_skill_space_ids as configured_user_skill_space_ids,
)


def skill_space_ids() -> list[str]:
    return configured_skill_space_ids()


def user_skill_space_ids() -> list[str]:
    return configured_user_skill_space_ids()


def public_skill_space_ids() -> list[str]:
    return configured_public_skill_space_ids()


def parse_space_ids(*raw_values: str) -> list[str]:
    return parse_skill_space_ids(*raw_values)


def dedupe_skill_refs(skill_refs: list[SkillRef], *, seen_names: set[str]) -> list[SkillRef]:
    selected: list[SkillRef] = []
    seen_keys: set[str] = set()
    for skill in skill_refs:
        name_key = skill.name.lower() if skill.name else ""
        key = skill.cache_key or skill.skill_id or skill.name
        if not name_key or name_key in seen_names or key in seen_keys:
            continue
        seen_names.add(name_key)
        seen_keys.add(key)
        selected.append(skill)
    return selected


def select_public_skill_refs(skill_refs: list[SkillRef]) -> list[SkillRef]:
    allowlist = {name.lower() for name in normalize_skill_names(os.environ.get("KSADK_PUBLIC_SKILL_ALLOWLIST", ""))}
    if not allowlist:
        return [skill for skill in skill_refs if skill.name]
    return [
        skill
        for skill in skill_refs
        if skill.name and skill.name.lower() in allowlist
    ]


def select_remote_skill_refs(
    skill_refs: list[SkillRef],
    prompt: str,
    *,
    skill_names: list[str] | None = None,
) -> list[SkillRef]:
    if not prompt and not normalize_skill_names(skill_names):
        return []
    return [match.skill for match in match_skill_refs(skill_refs, prompt, skill_names=skill_names)]


class SkillMatch:
    def __init__(self, skill: SkillRef, score: int, reason: str):
        self.skill = skill
        self.score = score
        self.reason = reason

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.skill.name,
            "description": self.skill.description,
            "version": self.skill.version,
            "score": self.score,
            "reason": self.reason,
            "aliases": list(self.skill.aliases),
            "tags": list(self.skill.tags),
        }


def match_skill_refs(
    skill_refs: list[SkillRef],
    prompt: str,
    *,
    skill_names: list[str] | None = None,
    limit: int | None = None,
) -> list[SkillMatch]:
    requested_names = {name.lower() for name in normalize_skill_names(skill_names)}
    normalized_prompt = prompt.lower()
    prompt_tokens = _tokens(normalized_prompt)
    matches: list[SkillMatch] = []
    seen: set[str] = set()
    for skill in skill_refs:
        if not skill.name:
            continue
        skill_name = skill.name.lower()
        if requested_names:
            if skill_name not in requested_names:
                continue
            score, reason = 100, "exact_name"
        else:
            score, reason = _metadata_score(skill, normalized_prompt, prompt_tokens)
            if score <= 0:
                continue
        key = skill.cache_key or skill.skill_id or skill.name
        if key in seen:
            continue
        seen.add(key)
        matches.append(SkillMatch(skill=skill, score=score, reason=reason))
    matches.sort(key=lambda item: (-item.score, item.skill.name.lower()))
    if limit is not None:
        return matches[: max(0, limit)]
    return matches


def _metadata_score(skill: SkillRef, normalized_prompt: str, prompt_tokens: set[str]) -> tuple[int, str]:
    if skill.name and skill.name.lower() in normalized_prompt:
        return 90, "name"
    for alias in skill.aliases:
        if alias.lower() in normalized_prompt:
            return 80, "alias"
    for tag in skill.tags:
        tag_key = tag.lower()
        if tag_key and (tag_key in normalized_prompt or tag_key in prompt_tokens):
            return 65, "tag"

    text = " ".join(
        [
            skill.description or "",
            " ".join(skill.examples),
        ]
    ).lower()
    skill_tokens = _tokens(text)
    overlap = prompt_tokens & skill_tokens
    if overlap:
        return min(55, 20 + len(overlap) * 10), "metadata"
    return 0, ""


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", value.lower())
        if len(token) >= 2
    }
