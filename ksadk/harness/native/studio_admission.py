"""Host-frozen Studio Build identity for opt-in Native runtime admission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from .state import Contract


def studio_mapping_digest(value: Mapping[str, Any]) -> str:
    """Hash only JSON configuration; opaque objects cannot silently pass admission."""
    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class StudioAdmission(Contract):
    build_id: str = Field(min_length=1)
    manifest_sha256: str
    model: str = Field(min_length=1)
    project_dir: str = Field(min_length=1)
    launch_config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
