"""Canonical A2A Message digest shared by request and control-plane contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from google.protobuf.json_format import MessageToDict

MAX_A2A_MESSAGE_BYTES = 1024 * 1024
MAX_A2A_MESSAGE_PARTS = 64
MAX_A2A_MESSAGE_ID_LENGTH = 128


def canonical_message_bytes(message: Any) -> bytes:
    """Serialize a pinned A2A protobuf using canonical lowerCamelCase JSON."""

    payload = MessageToDict(message, preserving_proto_field_name=False)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_message_sha256(message: Any) -> str:
    """Hash the logical Message, not transport or permit metadata."""

    return hashlib.sha256(canonical_message_bytes(message)).hexdigest()


__all__ = [
    "MAX_A2A_MESSAGE_BYTES",
    "MAX_A2A_MESSAGE_ID_LENGTH",
    "MAX_A2A_MESSAGE_PARTS",
    "canonical_message_bytes",
    "canonical_message_sha256",
]
