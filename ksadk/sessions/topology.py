"""Resolve the non-secret Session and framework checkpoint storage topology."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

StorageSource = Literal[
    "none",
    "explicit",
    "session_fallback",
    "checkpoint_fallback",
    "framework_override",
]


@dataclass(frozen=True)
class StorageTarget:
    """A logical persistent-storage target without connection diagnostics."""

    backend: str
    dsn: str = ""
    source: StorageSource = "none"
    explicit_local: bool = False


@dataclass(frozen=True)
class PersistenceTopology:
    """Effective Session and framework-native checkpoint storage targets."""

    session: StorageTarget
    checkpoint: StorageTarget
    framework: str


def _first_configured(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _session_backend(*, override: str | None = None) -> tuple[str, bool, bool]:
    raw = (
        override
        if override is not None
        else _first_configured(
            "KSADK_SESSION_BACKEND",
            "AGENTENGINE_SESSION_BACKEND",
            "KSADK_STM_BACKEND",
        )
    )
    normalized = raw.strip().lower()
    if normalized == "sqlite":
        normalized = "local"
    return (
        normalized or "local",
        normalized in {"local", "memory"},
        bool(normalized),
    )


def _framework_checkpoint_dsn(framework: str) -> str:
    if framework == "adk":
        return _first_configured("KSADK_ADK_SESSION_URL")
    if framework in {"langgraph", "langchain", "deepagents"}:
        return _first_configured("KSADK_LANGGRAPH_CHECKPOINT_DSN")
    return ""


def resolve_persistence_topology(
    *,
    framework: str | None = None,
    session_backend: str | None = None,
) -> PersistenceTopology:
    """Resolve dual persistence with documented Session/Checkpoint fallbacks.

    ``session_backend`` is an internal compatibility input for the legacy
    session resolver.  A caller explicitly selecting ``local``, ``sqlite`` or
    ``memory`` keeps that local choice even when a checkpoint DSN is present.
    """
    normalized_framework = (framework or "").strip().lower()
    backend, explicit_local, explicit_backend = _session_backend(override=session_backend)
    session_dsn = _first_configured(
        "KSADK_SESSION_DSN",
        "KSADK_STM_URL",
        "KSADK_STM_DB_URL",
    )
    checkpoint_dsn = _first_configured("KSADK_CHECKPOINT_DSN")
    framework_dsn = _framework_checkpoint_dsn(normalized_framework)

    if explicit_local:
        session = StorageTarget(backend=backend, explicit_local=True)
    elif session_dsn:
        session = StorageTarget(
            backend=backend if explicit_backend else "postgres",
            dsn=session_dsn,
            source="explicit",
        )
    elif checkpoint_dsn:
        session = StorageTarget(
            backend="postgres",
            dsn=checkpoint_dsn,
            source="checkpoint_fallback",
        )
    else:
        session = StorageTarget(backend=backend)

    if framework_dsn:
        checkpoint = StorageTarget(
            backend="postgres",
            dsn=framework_dsn,
            source="framework_override",
        )
    elif checkpoint_dsn:
        checkpoint = StorageTarget(backend="postgres", dsn=checkpoint_dsn, source="explicit")
    elif session.backend == "postgres" and session.dsn:
        checkpoint = StorageTarget(
            backend="postgres",
            dsn=session.dsn,
            source="session_fallback",
        )
    else:
        checkpoint = StorageTarget(backend="none")

    return PersistenceTopology(
        session=session,
        checkpoint=checkpoint,
        framework=normalized_framework,
    )


__all__ = ["PersistenceTopology", "StorageTarget", "resolve_persistence_topology"]
