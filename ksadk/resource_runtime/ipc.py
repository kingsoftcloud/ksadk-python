"""Bounded resource IPC framing shared by broker and worker pipes.

Transport owners enforce deadlines and connection permissions. No pickle,
reflection, imports or executable payloads are accepted by this protocol.
"""

from __future__ import annotations

import json
import math
import struct
from typing import BinaryIO, Literal

from pydantic import Field, field_validator

from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.contracts import Identifier

MAX_FRAME_BYTES = 1024 * 1024
ResourceOperation = Literal[
    "search_knowledge_base",
    "load_memory",
    "save_memory",
    "update_memory",
    "delete_memory",
    "memory_status",
    "list_skill_spaces",
    "list_skills",
    "search_skills",
    "load_skill",
    "read_skill_resource",
    "execute_skills",
    "read_skill_artifact",
]


class ResourceRequest(PluginContractModel):
    v: Literal[1]
    request_id: Identifier
    deadline: int = Field(strict=True, gt=0, description="Absolute Unix time in milliseconds")
    operation: ResourceOperation
    handle: str = Field(strict=True, min_length=32, max_length=256, repr=False)
    arguments: dict = Field(default_factory=dict)
    check_only: bool = Field(default=False, strict=True)

    @field_validator("v", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("IPC version must be an integer")
        return value


class FrameError(ValueError):
    """Public message is fixed and never includes frame contents."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise FrameError("Duplicate IPC object field")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise FrameError("Non-finite IPC number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise FrameError("Non-finite IPC number")
    return parsed


def encode_frame(payload: dict) -> bytes:
    if not isinstance(payload, dict):
        raise FrameError("IPC frame must contain an object")
    try:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise FrameError("Invalid IPC object") from None
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError("IPC frame exceeds size limit")
    return struct.pack("!I", len(body)) + body


def decode_body(body: bytes) -> dict:
    if not 0 < len(body) <= MAX_FRAME_BYTES:
        raise FrameError("Invalid IPC frame length")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except FrameError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise FrameError("Invalid IPC JSON") from None
    if not isinstance(payload, dict):
        raise FrameError("IPC frame must contain an object")
    return payload


def _read_exact(stream: BinaryIO, size: int, *, clean_eof: bool = False) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            if clean_eof and not chunks:
                return None
            raise FrameError("Truncated IPC frame")
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(stream: BinaryIO) -> dict | None:
    prefix = _read_exact(stream, 4, clean_eof=True)
    if prefix is None:
        return None
    (size,) = struct.unpack("!I", prefix)
    if not 0 < size <= MAX_FRAME_BYTES:
        raise FrameError("Invalid IPC frame length")
    body = _read_exact(stream, size)
    assert body is not None
    return decode_body(body)


def write_frame(stream: BinaryIO, payload: dict) -> None:
    frame = memoryview(encode_frame(payload))
    while frame:
        written = stream.write(frame)
        if written is None or written <= 0:
            raise FrameError("IPC transport write failed")
        frame = frame[written:]
    stream.flush()
