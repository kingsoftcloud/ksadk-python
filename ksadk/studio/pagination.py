"""Signed keyset cursors shared by Studio list endpoints.

The cursor is intentionally opaque to clients.  It binds the last stable sort
tuple to a namespace, a whitelisted sort name and the active filters so a
cursor cannot be replayed against a different query.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from ksadk.studio.errors import StudioError

T = TypeVar("T")
CursorScalar = str | int | float | bool | None

_CURSOR_VERSION = 1
_CURSOR_SECRET = secrets.token_bytes(32)
_MAX_CURSOR_LENGTH = 4096


def _invalid_cursor() -> StudioError:
    return StudioError(
        "PAGINATION_CURSOR_INVALID",
        "分页游标无效或已过期",
        status_code=422,
        field="cursor",
    )


def _encode_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_part(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise _invalid_cursor() from exc


def _filter_fingerprint(filters: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(filters),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:24]


def encode_cursor(
    *,
    namespace: str,
    sort: str,
    filters: Mapping[str, Any],
    key: Sequence[CursorScalar],
) -> str:
    """Return an authenticated opaque cursor for the last emitted row."""

    if not key or len(key) > 16:
        raise ValueError("cursor key must contain between 1 and 16 values")
    payload = {
        "v": _CURSOR_VERSION,
        "ns": namespace,
        "sort": sort,
        "filters": _filter_fingerprint(filters),
        "key": list(key),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(_CURSOR_SECRET, encoded, hashlib.sha256).digest()
    return f"{_encode_part(encoded)}.{_encode_part(signature)}"


def decode_cursor(
    cursor: str,
    *,
    namespace: str,
    sort: str,
    filters: Mapping[str, Any],
) -> tuple[CursorScalar, ...]:
    """Validate a cursor and return its stable sort tuple."""

    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise _invalid_cursor()
    try:
        encoded_part, signature_part = cursor.split(".", 1)
        encoded = _decode_part(encoded_part)
        signature = _decode_part(signature_part)
    except ValueError as exc:
        raise _invalid_cursor() from exc
    expected_signature = hmac.new(_CURSOR_SECRET, encoded, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected_signature):
        raise _invalid_cursor()
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid_cursor() from exc
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "ns",
        "sort",
        "filters",
        "key",
    }:
        raise _invalid_cursor()
    if (
        payload.get("v") != _CURSOR_VERSION
        or payload.get("ns") != namespace
        or payload.get("sort") != sort
        or payload.get("filters") != _filter_fingerprint(filters)
    ):
        raise _invalid_cursor()
    key = payload.get("key")
    if not isinstance(key, list) or not key or len(key) > 16:
        raise _invalid_cursor()
    if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in key):
        raise _invalid_cursor()
    return tuple(key)


def keyset_page(
    items: Sequence[T],
    *,
    key: Callable[[T], Sequence[CursorScalar]],
    reverse: bool,
    limit: int,
    cursor: str | None,
    namespace: str,
    sort: str,
    filters: Mapping[str, Any],
) -> dict[str, Any]:
    """Sort and page records using a unique stable tuple.

    Callers must include a unique identifier as the final key component.
    Filters are expected to have been applied before this function is called.
    """

    page_limit = max(1, limit)
    ordered = sorted(items, key=lambda item: tuple(key(item)), reverse=reverse)
    remaining = ordered
    if cursor is not None:
        cursor_key = decode_cursor(
            cursor,
            namespace=namespace,
            sort=sort,
            filters=filters,
        )
        try:
            remaining = [
                item
                for item in ordered
                if (tuple(key(item)) < cursor_key if reverse else tuple(key(item)) > cursor_key)
            ]
        except TypeError as exc:
            raise _invalid_cursor() from exc
    page_items = remaining[:page_limit]
    next_cursor = None
    if len(remaining) > page_limit and page_items:
        next_cursor = encode_cursor(
            namespace=namespace,
            sort=sort,
            filters=filters,
            key=key(page_items[-1]),
        )
    return {
        "items": page_items,
        "nextCursor": next_cursor,
        "total": len(ordered),
    }
