from __future__ import annotations

import os
from typing import Any

from ksadk.sandbox.backends.e2b import E2BSandboxBackend
from ksadk.sandbox.base import SandboxBackend, SandboxError, SandboxSpec, SandboxType


def bool_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def create_sandbox_backend(
    backend: str | None = None,
    *,
    sandbox_cls: Any | None = None,
) -> SandboxBackend:
    resolved = (backend or os.environ.get("KSADK_SANDBOX_BACKEND") or "e2b").strip().lower()
    if resolved != "e2b":
        raise SandboxError(f"Unsupported sandbox backend: {resolved}")
    return E2BSandboxBackend(spec=sandbox_spec_from_env(), sandbox_cls=sandbox_cls)


def sandbox_spec_from_env() -> SandboxSpec:
    template_id = (
        os.environ.get("KSADK_SANDBOX_TEMPLATE_ID")
        or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID")
        or ""
    )
    timeout = int(os.environ.get("KSADK_SANDBOX_TIMEOUT") or os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT") or "900")
    allow_internet_access = bool_env(
        "KSADK_SANDBOX_ALLOW_INTERNET_ACCESS",
        bool_env("KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS", True),
    )
    sandbox_type = SandboxType.from_value(os.environ.get("KSADK_SANDBOX_TYPE", "aio"))
    return SandboxSpec(
        template_id=template_id,
        sandbox_type=sandbox_type,
        timeout=timeout,
        allow_internet_access=allow_internet_access,
    )
