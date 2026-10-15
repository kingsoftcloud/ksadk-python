"""Structured ripgrep feedback for workspace search, independent of model text."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ksadk.toolsets.workspace_search_process import (
    SearchDeadlineExpired,
    SearchProcess,
    SearchRecordTooLarge,
)

SEARCH_TIMEOUT_SECONDS = 10


def search_with_rg(
    needle: str,
    base: Path,
    is_regex: bool,
    glob: str | None,
    case_sensitive: bool,
    context_lines: int,
    max_results: int,
    *,
    relative_path: Callable[[Path], str],
) -> dict[str, Any] | None:
    context = max(0, min(int(context_lines or 0), 20))
    limit = max(1, min(int(max_results or 100), 5000))
    # Pipes are block-buffered by default. Flush the first complete records
    # so a bounded prefix does not wait for the producer's unrelated tail.
    command = ["rg", "--no-config", "--json", "--line-buffered", "--color", "never"]
    if not is_regex:
        command.append("--fixed-strings")
    if not case_sensitive:
        command.append("--ignore-case")
    if glob:
        command.extend(["--glob", glob])
    if context:
        command.extend(["--context", str(context)])
    command.extend(["--", needle, str(base)])
    try:
        producer = SearchProcess(command, timeout=SEARCH_TIMEOUT_SECONDS)
    except OSError:
        # The optional executable may disappear after host discovery. Only an
        # unavailable producer selects the explicitly reported Python fallback.
        return None
    try:
        with producer:
            results, truncated = _matches(
                producer.lines(),
                context=context,
                limit=limit,
                relative_path=relative_path,
            )
            if not truncated:
                code, stderr = producer.finish()
                if code not in {0, 1}:
                    diagnostic = stderr.strip()[:1200]
                    invalid = (
                        "regex parse error" in diagnostic or "error parsing regex" in diagnostic
                    )
                    return {
                        "ok": False,
                        "error_code": "invalid_search_pattern"
                        if invalid
                        else "workspace_search_failed",
                        "error_message": diagnostic
                        or "Workspace search failed before a complete result.",
                    }
    except SearchDeadlineExpired:
        return {
            "ok": False,
            "error_code": "workspace_search_timeout",
            "error_message": "Workspace search exceeded its 10 second deadline; narrow the path.",
        }
    except SearchRecordTooLarge:
        return {
            "ok": False,
            "error_code": "workspace_search_record_too_large",
            "error_message": (
                "A search record exceeded 64 KiB; narrow the path or exclude large lines."
            ),
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return {
            "ok": False,
            "error_code": "workspace_search_output_invalid",
            "error_message": "Workspace search returned unreadable structured results.",
        }
    return {"ok": True, "query": needle, "results": results, "truncated": truncated}


def _matches(
    output: Iterable[str],
    *,
    context: int,
    limit: int,
    relative_path: Callable[[Path], str],
) -> tuple[list[dict[str, Any]], bool]:
    results: list[dict[str, Any]] = []
    recent: deque[tuple[int, str]] = deque(maxlen=context)
    pending: list[dict[str, Any]] = []
    current_path: str | None = None
    truncated = False
    # JSON escapes filename newlines; split only the producer's actual record
    # delimiter, never ':' or a Unicode line separator inside source text.
    for raw in output:
        if not raw:
            continue
        record = json.loads(raw)
        if record["type"] in {"begin", "end"}:
            recent.clear()
            pending.clear()
            current_path = None
            if truncated:
                break
            continue
        if record["type"] not in {"match", "context"}:
            continue
        data = record["data"]
        name = data["path"].get("text")
        text = data["lines"].get("text")
        if not isinstance(name, str) or not isinstance(text, str):
            # This tool exposes UTF-8 text, not ripgrep's binary byte records.
            continue
        try:
            path = relative_path(Path(name))
        except ValueError:
            continue
        line = data["line_number"]
        if type(line) is not int or line < 1:
            raise ValueError("invalid search line")
        if text.endswith("\n"):
            text = text[:-1]
            if text.endswith("\r"):
                text = text[:-1]
        if path != current_path:
            recent.clear()
            pending.clear()
            current_path = path
        pending = [row for row in pending if line <= row["line"] + context]
        for row in pending:
            if row["line"] < line:
                row["context_after"].append(text)
        pending = [row for row in pending if line < row["line"] + context]
        if record["type"] == "match":
            if len(results) == limit:
                truncated = True
            else:
                row = {
                    "path": path,
                    "line": line,
                    "text": text,
                    "snippet": text,
                    "context_before": [
                        value for number, value in recent if line - context <= number < line
                    ],
                    "context_after": [],
                }
                results.append(row)
                if context:
                    pending.append(row)
        recent.append((line, text))
        # Retained rows keep their complete requested context. Extra matching
        # lines can be context, but never raise the declared returned count.
        if truncated and not pending:
            break
    return results, truncated


def search_payload(
    query: str,
    path: str,
    results: list[dict[str, Any]],
    truncated: bool,
    context_lines: int,
    backend: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "query": query,
        "searched_path": path,
        "results": results,
        "match_count": len(results),
        "truncated": truncated,
        "context_lines": max(0, min(int(context_lines or 0), 20)),
        "search_backend": backend,
    }
