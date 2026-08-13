"""Server-owned local identifiers for Studio resources."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable

from ksadk.studio.errors import StudioError

_GENERATED_AGENT_SLUG = re.compile(r"^agentkit-[0-9a-f]{8}$")


def generate_agent_slug(exists: Callable[[str], bool]) -> str:
    """Allocate a short opaque Agent ID without trusting a client-side default."""

    for _attempt in range(32):
        candidate = f"agentkit-{secrets.token_hex(4)}"
        if not exists(candidate):
            return candidate
    raise StudioError(
        "AGENT_ID_EXHAUSTED",
        "无法分配唯一的本地标识",
        status_code=503,
        field="slug",
    )


def is_generated_agent_slug(value: str) -> bool:
    return bool(_GENERATED_AGENT_SLUG.fullmatch(str(value or "").strip()))


__all__ = ["generate_agent_slug", "is_generated_agent_slug"]
