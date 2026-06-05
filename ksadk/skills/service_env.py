from __future__ import annotations

import os

from ksadk.common.aicp_env import resolve_aicp_connection


def parse_skill_space_ids(*raw_values: str) -> list[str]:
    spaces: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in raw.split(","):
            space_id = part.strip()
            if not space_id or space_id in seen:
                continue
            seen.add(space_id)
            spaces.append(space_id)
    return spaces


def user_skill_space_ids() -> list[str]:
    return parse_skill_space_ids(
        os.environ.get("KSADK_SKILL_SPACE_IDS") or os.environ.get("SKILL_SPACE_ID") or ""
    )


def public_skill_space_ids() -> list[str]:
    return parse_skill_space_ids(os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS") or "")


def skill_space_ids() -> list[str]:
    return parse_skill_space_ids(
        os.environ.get("KSADK_SKILL_SPACE_IDS") or os.environ.get("SKILL_SPACE_ID") or "",
        os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS") or "",
    )


def resolve_skill_service_url(*, require_spaces: bool = True) -> str:
    explicit = os.environ.get("KSADK_SKILL_SERVICE_URL", "").strip()
    if explicit:
        return explicit
    if require_spaces and not skill_space_ids():
        return ""

    connection = resolve_aicp_connection("KSADK_SKILL_SERVICE")
    return f"{connection['scheme']}://{connection['endpoint']}".rstrip("/")


def should_resolve_child_skill_service_url() -> bool:
    if os.environ.get("KSADK_SKILL_SERVICE_URL", "").strip():
        return True
    if os.environ.get("KSADK_SKILL_SERVICE_ENDPOINT", "").strip():
        return True
    if os.environ.get("KSADK_SKILL_SERVICE_SCHEME", "").strip():
        return True

    mode = os.environ.get("KSADK_AICP_ENDPOINT_MODE", "").strip().lower()
    return mode not in {"", "auto", "detect"}
