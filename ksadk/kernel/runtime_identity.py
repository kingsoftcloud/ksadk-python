"""Non-secret provenance for the KsADK code that is actually imported.

The base runtime image may contain an older ``ksadk`` distribution while a
Code deployment shadows it from ``/app/code``.  Health must therefore report
the source package identity, never the base image's distribution metadata.

``_bundle_identity.py`` is generated into Code archives by :class:`CodeBuilder`.
It is package content, not a user environment variable, and is intentionally
optional so legacy images report an honest incomplete provenance record.
"""

from __future__ import annotations

import importlib
import re
from functools import lru_cache
from typing import Any

from ksadk.version import VERSION

_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@lru_cache(maxsize=1)
def runtime_identity() -> dict[str, str]:
    """Return the provenance embedded beside the imported KsADK package.

    Missing or malformed optional provenance is represented by an empty field;
    callers must not substitute process environment values or an image tag.
    """

    identity: dict[str, str] = {
        "ksadk_version": VERSION,
        "ksadk_commit": "",
        "ksadk_source_digest": "",
    }
    try:
        bundled: Any = importlib.import_module("ksadk._bundle_identity")
        candidate = getattr(bundled, "BUNDLE_IDENTITY", {})
    except (ImportError, AttributeError):
        candidate = {}
    if not isinstance(candidate, dict):
        return identity

    # Do not let a stale identity module claim a version other than the code
    # currently imported by Python.
    if str(candidate.get("ksadk_version") or "") != VERSION:
        return identity
    commit = str(candidate.get("ksadk_commit") or "").lower()
    digest = str(candidate.get("ksadk_source_digest") or "").lower()
    if _COMMIT_RE.fullmatch(commit):
        identity["ksadk_commit"] = commit
    if _SHA256_RE.fullmatch(digest):
        identity["ksadk_source_digest"] = digest
    return identity


__all__ = ["runtime_identity"]
