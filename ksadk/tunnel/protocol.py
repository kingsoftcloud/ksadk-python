"""Frame limits and errors for the streaming HTTP tunnel."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

LOCAL_ERROR_CODES = frozenset(
    {
        "busy",
        "forbidden",
        "invalid_request",
        "local_mcp_error",
        "local_service_error",
        "timeout",
        "frame_too_large",
        "too_many_tools",
    }
)

VERSION = 2
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class TunnelError(RuntimeError):
    """A sanitized tunnel failure; a failed call may already have taken effect."""

    def __init__(self, code: str, *, indeterminate: bool = False):
        self.code = code
        self.indeterminate = indeterminate
        super().__init__(f"Tool tunnel: {code}" + (" (outcome unknown)" if indeterminate else ""))


class IncompletePeerDiscovery(TunnelError):
    """No services were confirmed because one or more peers were unreachable."""

    def __init__(self):
        super().__init__("peer_discovery_unavailable")


@dataclass(frozen=True)
class TunnelLimits:
    max_frame_bytes: int = 1_048_576
    max_response_bytes: int = 8_388_608
    max_inflight: int = 32
    max_services: int = 16
    max_tools: int = 256
    registration_timeout: float = 10.0
    request_timeout: float = 60.0
    send_timeout: float = 5.0
    heartbeat_interval: float = 15.0
    idle_timeout: float = 45.0

    def __post_init__(self):
        for name, value in vars(self).items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                or (name.startswith("max_") and not isinstance(value, int))
            ):
                raise ValueError(f"{name} must be finite and positive (limits require integers)")
        if self.idle_timeout <= self.heartbeat_interval:
            raise ValueError("idle_timeout must exceed heartbeat_interval")


def identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a 1-64 character identifier")
    return value


def decode_request_body(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise TunnelError("invalid_request") from None


def encode(frame: dict[str, Any], limits: TunnelLimits) -> str:
    raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > limits.max_frame_bytes:
        raise TunnelError("frame_too_large")
    return raw


def decode(raw: str, limits: TunnelLimits) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > limits.max_frame_bytes:
        raise ValueError("invalid frame size or encoding")
    frame = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NaN")))
    if not isinstance(frame, dict):
        raise ValueError("frame must be an object")
    return frame
