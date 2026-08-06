from __future__ import annotations

import logging
from typing import Any

AUTOCOMPACT_KEEP_TAIL_GROUPS = 4
PTL_RETRY_KEEP_TAIL_GROUPS = 2
PROMPT_TOO_LONG_MARKERS = (
    "prompt-too-long",
    "prompt too long",
    "maximum context length",
    "context length",
    "context_length_exceeded",
    "413",
)
SESSION_SUMMARY_MAX_CHARS = 160
ATTACHMENT_CONTEXT_STATE_KEY = "__ksadk_attachment_context__"
EVENT_SCAN_PAGE_SIZE = 500
# Durable stream snapshots make a detached run recoverable after a browser refresh.
# Keep them bounded: the first text delta is persisted immediately, then at most one
# update per interval unless a substantial amount of text has arrived.
ASSISTANT_STREAM_SNAPSHOT_INTERVAL_SECONDS = 0.5
ASSISTANT_STREAM_SNAPSHOT_MIN_NEW_CHARS = 256

logger = logging.getLogger("ksadk.conversations.runtime")
_MODEL_CATALOG_CACHE_TTL_SECONDS = 60.0
_MODEL_CATALOG_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
