"""Lossless, source-first archive framing, separate from execution receipts.

This module performs no I/O and grants no authority. The host must explicitly
bind the source archive policy and validate the returned ArtifactRef itself.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

CAPTURED_SOURCE_MIME = "application/vnd.ksadk.captured-source+json"
CAPTURED_SOURCE_VERSION = "captured-source-artifact/v1"


def captured_source(content):
    if not isinstance(content, Mapping) or content.get("ok") is not True:
        return None
    carrier = content.get("requested_source")
    if not isinstance(carrier, Mapping) or set(carrier) != {"path", "read_range", "content"}:
        return None
    path, span, body = carrier["path"], carrier["read_range"], carrier["content"]
    if (
        not isinstance(path, str) or not path or content.get("path") != path
        or not isinstance(body, str) or not body
        or not isinstance(span, dict) or set(span) != {"start", "end"}
        or any(type(v) is not int for v in span.values())
        or not 1 <= span["start"] <= span["end"]
        or len(body.splitlines()) != span["end"] - span["start"] + 1
    ):
        return None
    return body, path, {"start": span["start"], "end": span["end"]}


def encode_captured_source(content):
    source = captured_source(content)
    if source is None:
        return None
    body, path, span = source
    # content sorts first: even a small character page starts with source, not
    # receipt counters or a duplicated truncated preview. Body bytes and line
    # numbering are unchanged; offsets address this declared JSON document.
    return json.dumps({
        "version": CAPTURED_SOURCE_VERSION, "content": body, "path": path, "read_range": span,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate source archive field")
        result[key] = value
    return result


def decode_captured_source(text):
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
        if (not isinstance(value, dict)
            or set(value) != {"version", "content", "path", "read_range"}
            or value["version"] != CAPTURED_SOURCE_VERSION):
            return None
        return captured_source({"ok": True, "path": value["path"], "requested_source": {
            key: value[key] for key in ("content", "path", "read_range")
        }})
    except (ValueError, TypeError):
        return None
