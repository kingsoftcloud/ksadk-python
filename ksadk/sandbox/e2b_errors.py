"""Recognize E2B transport failures without trusting the SDK's timeout wrapper."""

from __future__ import annotations

import httpx

from ksadk.sandbox.diagnostics import exception_chain


def is_e2b_unavailable(exc: Exception) -> bool:
    # Keep E2B optional for SDK users of other sandbox backends.
    try:
        import e2b_connect
    except ImportError:
        e2b_connect = None

    for item in exception_chain(exc):
        if (
            e2b_connect is not None
            and isinstance(item, e2b_connect.ConnectException)
            and item.status == e2b_connect.Code.unavailable
        ):
            return True
        if isinstance(item, httpx.HTTPStatusError) and item.response.status_code in (502, 503):
            return True
    return False
