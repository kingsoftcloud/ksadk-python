"""Derive source navigation from canonical, host-labeled tool messages.

Only a host-granted workspace reader may supply the range protocol. Its data
is an observation, not semantic coverage or a fresh authorization to edit.
No disk reads, model calls or parsing of compaction prose occurs here.
"""

import hashlib
import json
from collections.abc import Iterator

from .context import SourceReadProgress
from .context_pruning import project_workspace_read_result
from .ports import ToolDefinition
from .state import RunSnapshot


def source_path_navigation(reads: list[dict]) -> dict:
    """Keep older exact locators alongside recent ranges, within 2 KiB.

    OpenCode's fixed tool-history projection preserves original tool input
    after output pruning. Here canonical read receipts, rather than lossy
    model summary prose, supply the equivalent path observation. This does
    not attest current file contents or semantic coverage.
    """
    navigation = {
        "authority": "navigation_only",
        "source_freshness": "not_attested",
        "semantic_coverage": "not_attested",
        "paths": [],
        "omitted_paths": len(reads),
    }
    # The recent-eight range view already covers the newest reads. Start at
    # the oldest so repeated compactions do not silently erase their identity.
    for item in reads:
        navigation["paths"].append(item["path"])
        navigation["omitted_paths"] -= 1
        if len(json.dumps(navigation, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()) > 2048:
            navigation["paths"].pop()
            navigation["omitted_paths"] += 1
    return navigation


def _range(source):
    start, end, total, count = (
        source.get(k) for k in ("start_line", "end_line", "total_lines", "line_count")
    )
    declared = source.get("read_range")
    if (
        any(type(v) is not int for v in (start, end, total, count))
        or not 1 <= start <= end <= total
        or count != end - start + 1
        or not isinstance(declared, dict)
        or any(type(declared.get(k)) is not int for k in ("start", "end"))
        or declared != {"start": start, "end": end}
        or not isinstance(source.get("content"), str)
        or len(source["content"].split("\n")) != count
    ):
        return None
    return start, end


def _source_read_pages(
    snapshot: RunSnapshot, tools: tuple[ToolDefinition, ...], *, max_chars: int | None
) -> Iterator[SourceReadProgress]:
    names = {t.name for t in tools if t.read_only and t.context_role == "workspace_read"}
    calls = {}
    for message in snapshot.messages:
        if message.get("role") == "assistant":
            # A result must belong to the preceding declared tool group.
            calls = {}
            for call in message.get("tool_calls") or ():
                try:
                    if call["function"]["name"] in names:
                        args = json.loads(call["function"]["arguments"])
                        path = args.get("path")
                        if isinstance(path, str) and path.strip() and len(path) <= 2048:
                            calls[call["id"]] = path
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
            continue
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        path = calls.get(call_id)
        ref, cursor = message.get("receipt_ref"), message.get("receipt_tool_call")
        if (
            not path
            or not isinstance(ref, str)
            or not ref.startswith(f"{snapshot.run_id}:effect:")
            or type(cursor) is not int
            or not 0 < cursor <= snapshot.tool_calls
                or type(message.get("receipt_goal_revision")) is not int
                or message.get("receipt_goal_revision") != snapshot.goal_revision
            or not isinstance(message.get("content"), str)
        ):
            continue
        try:
            receipt = json.loads(message["content"])
            source = receipt.get("content")
            if (
                receipt.get("status") != "ok"
                or not isinstance(source, dict)
                or source.get("ok") is not True
                or source.get("path") != path
                or not isinstance(source.get("content"), str)
            ):
                continue
            returned = _range(source)
            if returned is None:
                continue
            view = (
                project_workspace_read_result(message, path=path, max_chars=max_chars)
                if max_chars is not None
                else message
            )
            if view is None:
                continue
            visible = json.loads(view["content"])["content"]
            main = _range(visible) if isinstance(visible.get("content"), str) else None
            item = SourceReadProgress(
                path=path,
                receipt_ref=ref,
                tool_call_id=call_id,
                returned_lines=returned,
                configured_prefix_lines=main,
                configured_next_start_line=(
                    main[1] + 1 if main and main[1] < source["total_lines"] else None
                ),
                total_lines=source["total_lines"],
                source_mtime_ns=(
                    source.get("mtime_ns") if type(source.get("mtime_ns")) is int else None
                ),
                source_size_bytes=(
                    source.get("size_bytes") if type(source.get("size_bytes")) is int else None
                ),
                source_payload_digest="sha256:"
                + hashlib.sha256(source["content"].encode("utf-8")).hexdigest(),
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        yield item


def source_read_progress(
    snapshot: RunSnapshot, tools: tuple[ToolDefinition, ...], *, max_chars: int | None
) -> tuple[SourceReadProgress, ...]:
    # Preserve the v2/v3 latest-page contract and ordering byte for byte.
    progress = {}
    for item in _source_read_pages(snapshot, tools, max_chars=max_chars):
        path = item.path
        progress.pop(path, None)
        progress[path] = item
    return tuple(progress.values())


def source_read_history(
    snapshot: RunSnapshot, tools: tuple[ToolDefinition, ...], *, max_chars: int | None
) -> tuple[SourceReadProgress, ...]:
    """One earlier distinct, farthest returned page per path; never a union.

    A reread of a header must not erase the locator of an earlier body page.
    These are historical receipts, possibly from different file versions, not
    current coverage, model observation, or a cursor to skip unread contents.
    """
    pages = tuple(_source_read_pages(snapshot, tools, max_chars=max_chars))
    latest = {item.path: item for item in pages}
    earlier = {}
    for item in pages:
        current = latest[item.path]
        if (item.returned_lines, item.source_payload_digest) == (
            current.returned_lines, current.source_payload_digest
        ):
            continue
        prior = earlier.get(item.path)
        if prior is None or item.returned_lines[::-1] >= prior.returned_lines[::-1]:
            earlier[item.path] = item
    return tuple(earlier.values())


def source_read_pages(
    snapshot: RunSnapshot, tools: tuple[ToolDefinition, ...], *, max_chars: int | None
) -> tuple[SourceReadProgress, ...]:
    """Retain individual receipt identities, including first and middle pages.

    Page ranges are never merged: receipts may describe different file versions
    and host-returned text does not attest model observation or understanding.
    """
    return tuple(_source_read_pages(snapshot, tools, max_chars=max_chars))


def source_page_index_navigation(pages: list[dict]) -> dict:
    """Project independent historical ranges within the existing 2 KiB bound."""
    navigation = {
        "authority": "navigation_only",
        "observation_scope": "independent_historical_pages_not_cumulative_coverage",
        "source_freshness": "not_attested",
        "same_file_version": "not_attested",
        "semantic_coverage": "not_attested",
        "model_observation": "not_attested",
        "page_count": len(pages),
        "pages_digest": hashlib.sha256(json.dumps(
            pages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
        "columns": ["path", "independent_returned_line_pages"],
        "pages": [],
        "omitted_pages": len(pages),
    }
    grouped = {}
    for item in pages:
        grouped.setdefault(item["path"], []).append(item["returned_lines"])

    def fits():
        return len(json.dumps(navigation, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode()) <= 2048

    records = []
    for path, ranges in grouped.items():
        row = [path, [ranges[0]]]
        navigation["pages"].append(row)
        navigation["omitted_pages"] -= 1
        if not fits():
            navigation["pages"].pop()
            navigation["omitted_pages"] += 1
            continue
        records.append((row, ranges))
    # Give every admitted path an early page before filling its middle pages.
    # This is a bounded index, not a cursor telling the model to skip contents.
    for index in range(1, max((len(ranges) for _, ranges in records), default=0)):
        added = False
        for row, ranges in records:
            if index >= len(ranges):
                continue
            row[1].append(ranges[index])
            navigation["omitted_pages"] -= 1
            if not fits():
                row[1].pop()
                navigation["omitted_pages"] += 1
            else:
                added = True
        if not added:
            break
    navigation["pages"] = [row for row in navigation["pages"] if row[1]]
    return navigation


def source_page_history_navigation(history: list[dict], latest: list[dict]) -> dict:
    """Bound independent historical-page locators to 2 KiB, without merging."""
    navigation = {
        "authority": "navigation_only",
        "observation_scope": "independent_historical_pages_not_cumulative_coverage",
        "source_freshness": "not_attested",
        "same_file_version": "not_attested",
        "semantic_coverage": "not_attested",
        "model_observation": "not_attested",
        "history_count": len(history),
        "history_digest": hashlib.sha256(json.dumps(
            history, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
        "columns": ["path", "earlier_returned_lines", "earlier_receipt_ref",
                    "latest_returned_lines", "latest_receipt_ref"],
        "pages": [],
        "omitted_paths": len(history),
    }
    current = {item["path"]: item for item in latest}
    for item in history:
        last = current[item["path"]]
        navigation["pages"].append([
            item["path"], item["returned_lines"], item["receipt_ref"],
            last["returned_lines"], last["receipt_ref"],
        ])
        navigation["omitted_paths"] -= 1
        if len(json.dumps(navigation, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()) > 2048:
            navigation["pages"].pop()
            navigation["omitted_paths"] += 1
    return navigation
