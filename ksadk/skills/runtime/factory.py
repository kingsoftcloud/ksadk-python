from __future__ import annotations

import os

from ksadk.skills.runtime.base import SkillRuntimeBackend
from ksadk.skills.runtime.backends.disabled import DisabledSkillRuntimeBackend
from ksadk.skills.runtime.backends.e2b import E2BSkillRuntimeBackend
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend


def _resolve_backend(backend: str | None = None) -> str:
    if backend is not None:
        return backend.strip().lower()

    explicit = os.environ.get("KSADK_SKILL_RUNTIME_BACKEND")
    if explicit is not None:
        return explicit.strip().lower()

    sandbox_backend = os.environ.get("KSADK_SANDBOX_BACKEND", "").strip().lower()
    if sandbox_backend and sandbox_backend not in {"disabled", "none", "off"}:
        return sandbox_backend

    if os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID"):
        return "e2b"

    return "disabled"


def create_skill_runtime_backend(backend: str | None = None) -> SkillRuntimeBackend:
    resolved = _resolve_backend(backend)
    if resolved in {"", "disabled", "none", "off"}:
        return DisabledSkillRuntimeBackend()
    if resolved == "local_process":
        return LocalProcessSkillRuntimeBackend.from_env()
    if resolved == "e2b":
        return E2BSkillRuntimeBackend.from_env()
    raise ValueError(f"Unsupported skill runtime backend: {resolved}")
