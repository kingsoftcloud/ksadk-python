"""Non-secret provenance for the KsADK code that is actually imported.

The base runtime image may contain an older ``ksadk`` distribution while a
Code deployment shadows it from ``/app/code``.  Health must therefore report
the source package identity, never the base image's distribution metadata.

``_bundle_identity.py`` is generated into Code archives by :class:`CodeBuilder`.
It is package content, not a user environment variable, and is intentionally
optional so legacy images report an honest incomplete provenance record.

The managed-runtime image also attests the exact wheel it installs.  That
image provenance is used only when Python is importing KsADK from the image's
``site-packages`` directory.  A Code archive that shadows KsADK must carry its
own bundle identity; an environment variable from the base image must never
claim provenance for user-supplied code.
"""

from __future__ import annotations

import importlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ksadk.version import VERSION

_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _imports_image_installed_ksadk() -> bool:
    """Whether ``ksadk`` is the distribution installed by the runtime image."""

    try:
        package = importlib.import_module("ksadk")
        package_file = Path(str(package.__file__ or "")).resolve()
    except (ImportError, OSError, RuntimeError):
        return False
    return "site-packages" in package_file.parts


def _image_identity() -> dict[str, str]:
    """Read image-attested provenance without trusting it for shadowed code."""

    if not _imports_image_installed_ksadk():
        return {}
    return {
        "ksadk_commit": str(
            os.environ.get("KSADK_RUNTIME_IMAGE_SOURCE_COMMIT") or ""
        ).lower(),
        "ksadk_wheel_sha256": str(
            os.environ.get("KSADK_RUNTIME_IMAGE_WHEEL_SHA256") or ""
        ).lower(),
    }


@lru_cache(maxsize=1)
def runtime_identity() -> dict[str, str]:
    """Return the provenance embedded beside the imported KsADK package.

    Missing or malformed optional provenance is represented by an empty field.
    Image provenance is accepted only for the image-installed distribution;
    callers must not substitute a process environment value for Code-bundled
    KsADK or infer identity from an image tag.
    """

    identity: dict[str, str] = {
        "ksadk_version": VERSION,
        "ksadk_commit": "",
        "ksadk_source_digest": "",
        "ksadk_wheel_sha256": "",
    }
    try:
        bundled: Any = importlib.import_module("ksadk._bundle_identity")
        candidate = getattr(bundled, "BUNDLE_IDENTITY", {})
    except (ImportError, AttributeError):
        candidate = {}
    if isinstance(candidate, dict) and str(candidate.get("ksadk_version") or "") == VERSION:
        # A Code bundle is the closest provenance of the code Python imported.
        commit = str(candidate.get("ksadk_commit") or "").lower()
        digest = str(candidate.get("ksadk_source_digest") or "").lower()
        if _COMMIT_RE.fullmatch(commit):
            identity["ksadk_commit"] = commit
        if _SHA256_RE.fullmatch(digest):
            identity["ksadk_source_digest"] = digest
        if identity["ksadk_commit"] or identity["ksadk_source_digest"]:
            return identity

    # No valid bundle identity means the installed image distribution is the
    # imported source only when it has not been shadowed by a Code archive.
    image = _image_identity()
    commit = image.get("ksadk_commit", "")
    wheel_digest = image.get("ksadk_wheel_sha256", "")
    if _COMMIT_RE.fullmatch(commit):
        identity["ksadk_commit"] = commit
    if _SHA256_RE.fullmatch(wheel_digest):
        identity["ksadk_wheel_sha256"] = wheel_digest
    return identity


__all__ = ["runtime_identity"]
