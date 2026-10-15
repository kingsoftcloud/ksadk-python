"""Recognize complete, host-authorized source-search observations for a quote.

The caller locks the tool's context role and obtains arguments from its real
call. This only retains returned source data; it grants no search completeness,
file freshness, execution, editing or semantic-verification authority.
"""

from __future__ import annotations

import json

from .context_history import _unambiguous_json_object


def complete_search_call_ids(messages, *, tool_names):
    """Recognize successful replies using calls in one actual closed batch."""
    calls = {
        call["id"]: call["function"]
        for raw in messages
        for call in raw.get("tool_calls", ())
        if call["function"]["name"] in tool_names
    }
    return frozenset(
        raw["tool_call_id"]
        for raw in messages
        if raw.get("role") == "tool"
        and (function := calls.get(raw.get("tool_call_id"))) is not None
        and complete_search_observation(
            raw, arguments=_unambiguous_json_object(function.get("arguments"))
        )
        is not None
    )


def complete_search_observation(message, *, arguments):
    if message.get("role") != "tool" or not isinstance(arguments, dict):
        return None
    query, path = arguments.get("query"), arguments.get("path", ".")
    if not isinstance(query, str) or not query.strip() or not isinstance(path, str):
        return None
    receipt = _unambiguous_json_object(message.get("content"))
    if receipt is None or receipt.get("status") != "ok":
        return None
    source = receipt.get("content")
    if isinstance(source, dict) and source.get("observation_type") == "scoped-artifact-search/v1":
        uris, rows = arguments.get("uris"), source.get("results")
        if (
            not isinstance(uris, list)
            or not uris
            or len(uris) > 8
            or any(not isinstance(uri, str) or not uri.startswith("artifact://") for uri in uris)
            or len(set(uris)) != len(uris)
            or source.get("query") != query
            or type(source.get("complete")) is not bool
            or not isinstance(rows, list)
            or [r.get("uri") if isinstance(r, dict) else None for r in rows] != uris
        ):
            return None
        for row in rows:
            if type(row.get("complete")) is not bool:
                return None
            if row.get("code") in {"artifact_search_failed", "artifact_search_too_large"}:
                if row["complete"]:
                    return None
                continue
            if row.get("coordinate_space") not in {"artifact", "captured_source"}:
                return None
            matches = row.get("matches")
            if not isinstance(matches, list) or len(matches) > 16:
                return None
            for match in matches:
                if (
                    not isinstance(match, dict)
                    or type(match.get("offset_chars")) is not int
                    or match["offset_chars"] < 0
                    or not isinstance(match.get("excerpt"), str)
                    or type(match.get("excerpt_truncated")) is not bool
                ):
                    return None
        if source["complete"] != all(r["complete"] for r in rows):
            return None
        # Preserve the entire declared observation, including omissions. This
        # does not validate source freshness, grant a Ref or prove semantics.
        return json.loads(json.dumps(message, ensure_ascii=False))
    if (
        not isinstance(source, dict)
        or source.get("ok") is not True
        or source.get("query") != query
        or source.get("searched_path") != path
        or type(source.get("truncated")) is not bool
        or type(source.get("match_count")) is not int
        or not isinstance(source.get("results"), list)
        or source["match_count"] != len(source["results"])
    ):
        return None
    for row in source["results"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("path"), str)
            or not row["path"]
            or type(row.get("line")) is not int
            or row["line"] < 1
            or not isinstance(row.get("text"), str)
        ):
            return None
        for field in ("context_before", "context_after"):
            context = row.get(field, [])
            if not isinstance(context, list) or not all(isinstance(line, str) for line in context):
                return None
    # Return all original fields and exact payload bytes, not selected matches.
    # Parsing is only recognition; the raw message still supplies identity.
    return json.loads(json.dumps(message, ensure_ascii=False))
