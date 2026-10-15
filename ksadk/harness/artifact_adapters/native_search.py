"""Bounded literal search of a Run's already earned artifact references.

This is the scoped equivalent of searching persisted tool output. It neither
opens workspace paths nor interprets task semantics or verifies delivery.
"""

from __future__ import annotations

import hashlib
import json

from ksadk.harness.native.artifact_search_contract import (
    ARTIFACT_SEARCH_DEFINITION as ARTIFACT_SEARCH_DEFINITION,
)
from ksadk.harness.native.artifact_search_contract import (
    ARTIFACT_SEARCH_NAME as ARTIFACT_SEARCH_NAME,
)
from ksadk.harness.native.ports import (
    ArtifactPort,
    ArtifactReadRequest,
    ToolPort,
    ToolRequest,
    ToolResult,
)
from ksadk.harness.native.tool_recovery import query_sealed_tool_result
from ksadk.tools.captured_source_artifact import CAPTURED_SOURCE_MIME, decode_captured_source

from .native_search_budget import search_definition

_MAX_SCAN_BYTES = 1024 * 1024
_MAX_EXCERPT_CHARS = 8192


def _argument_error(args):
    """Diagnose only public schema constraints, never argument values or keys."""
    def failure(field, constraint, expected, *, items=False):
        return {
            "code": "invalid_artifact_search",
            "constraint": constraint,
            "schema_path": (
                f"/properties/{field}" + ("/items" if items else "") + f"/{constraint}"
                if field and constraint != "required" else f"/{constraint}"
            ),
            **({"field": field} if field else {}),
            ("expected_type" if constraint == "type" else constraint): expected,
            "instruction": "Correct arguments against this tool schema before retrying.",
        }

    if set(args).difference({"uris", "query", "max_matches", "context_chars"}):
        return failure(None, "additionalProperties", False)
    for field in ("uris", "query"):
        if field not in args:
            return failure(field, "required", True)
    uris, query = args["uris"], args["query"]
    if not isinstance(uris, list):
        return failure("uris", "type", "array")
    if not 1 <= len(uris) <= 8:
        return failure("uris", "minItems", 1) if not uris else failure("uris", "maxItems", 8)
    for uri in uris:
        if not isinstance(uri, str):
            return failure("uris", "type", "string", items=True)
        if not uri:
            return failure("uris", "minLength", 1, items=True)
    if len(set(uris)) != len(uris):
        return failure("uris", "uniqueItems", True)
    if not isinstance(query, str):
        return failure("query", "type", "string")
    if not 1 <= len(query) <= 1024:
        return (
            failure("query", "minLength", 1)
            if not query else failure("query", "maxLength", 1024)
        )
    for field, default, low, high in (("max_matches", 16, 1, 16), ("context_chars", 120, 0, 256)):
        value = args.get(field, default)
        if type(value) is not int:
            return failure(field, "type", "integer")
        if value < low:
            return failure(field, "minimum", low)
        if value > high:
            return failure(field, "maximum", high)
    return None


def _source(text, mime_type=None):
    """Decode a known source carrier; its metadata remains untrusted tool data."""
    if mime_type == CAPTURED_SOURCE_MIME:
        source = decode_captured_source(text)
        if source is not None:
            body, path, span = source
            return body, path, span["start"]
        return text, None, None
    try:
        raw = json.loads(text)["content"]
        carrier = raw["requested_source"]
        path, span, body = carrier["path"], carrier["read_range"], carrier["content"]
        if (
            raw.get("ok") is True
            and raw.get("path") == path
            and isinstance(path, str)
            and isinstance(body, str)
            and body
            and isinstance(span, dict)
            and set(span) == {"start", "end"}
            and all(type(v) is int for v in span.values())
            and 1 <= span["start"] <= span["end"]
            and len(body.splitlines()) == span["end"] - span["start"] + 1
        ):
            return body, path, span["start"]
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return text, None, None


class NativeArtifactSearchPort:
    def __init__(self, delegate: ToolPort, artifacts: ArtifactPort, *,
                 search_policy=None, definitions=()):
        self.delegate, self.artifacts = delegate, artifacts
        self.search_policy, self.definitions = search_policy, definitions

    async def _query(self, request, method):
        return await query_sealed_tool_result(
            self.delegate,
            request,
            owned_names=(ARTIFACT_SEARCH_NAME,),
            method=method,
        )

    async def reconcile(self, request):
        return await self._query(request, "reconcile")

    async def recover_prepared(self, request):
        return await self._query(request, "recover_prepared")

    async def resume_prepared(self, request):
        return await self._query(request, "resume_prepared")

    async def resume_checkpoint(self, request):
        return await self._query(request, "resume_checkpoint")

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.call.name != ARTIFACT_SEARCH_NAME:
            return await self.delegate.execute(request)
        if request.definition != search_definition(self.search_policy):
            return ToolResult(status="error", content={"code": "invalid_artifact_search"})
        args = request.call.arguments
        error = _argument_error(args)
        if error is not None:
            return ToolResult(status="error", content=error)
        uris, query = args.get("uris"), args.get("query")
        limit, context = args.get("max_matches", 16), args.get("context_chars", 120)
        allowed = {ref.uri: ref for ref in request.available_artifacts}
        if any(uri not in allowed for uri in uris):
            return ToolResult(status="error", content={"code": "artifact_not_available"})
        budget = None
        if self.search_policy is not None:
            try:
                budget = self.search_policy.resolve(request, self.definitions)
            except ValueError:
                return ToolResult(
                    status="error", content={"code": "artifact_search_capacity_unavailable"},
                )
        results, fingerprints, remaining, characters = [], [], limit, _MAX_EXCERPT_CHARS
        for uri in uris:
            ref = allowed[uri]
            if ref.size_bytes > _MAX_SCAN_BYTES:
                results.append({"uri": uri, "complete": False, "code": "artifact_search_too_large"})
                continue
            try:
                chunks, offset = [], 0
                while True:
                    page = await self.artifacts.read(
                        ArtifactReadRequest(
                            scope=request.scope,
                            run_id=request.run_id,
                            ref=ref,
                            offset_chars=offset,
                            limit_chars=32000,
                        )
                    )
                    if (
                        page.ref != ref
                        or page.offset_chars != offset
                        or page.next_offset_chars != offset + len(page.content)
                        or len(page.content) > 32000
                        or (not page.complete and not page.content)
                    ):
                        raise ValueError("artifact page mismatch")
                    chunks.append(page.content)
                    offset = page.next_offset_chars
                    if offset > _MAX_SCAN_BYTES:
                        raise ValueError("artifact scan limit")
                    if page.complete:
                        break
                text = "".join(chunks)
                encoded = text.encode("utf-8")
                if len(encoded) != ref.size_bytes or (
                    "sha256:" + hashlib.sha256(encoded).hexdigest() != ref.content_hash
                ):
                    raise ValueError("artifact content mismatch")
            except Exception:
                results.append({"uri": uri, "complete": False, "code": "artifact_search_failed"})
                continue
            body, path, start = _source(text, ref.mime_type)
            matches, cursor = [], 0
            while (found := body.find(query, cursor)) >= 0 and remaining and characters:
                excerpt = body[max(0, found - context) : found + len(query) + context]
                clipped = len(excerpt) > characters
                excerpt = excerpt[:characters]
                matches.append(
                    {
                        "offset_chars": found,
                        "excerpt": excerpt,
                        "excerpt_truncated": clipped,
                        **({"source_line": start + body.count("\n", 0, found)} if start else {}),
                    }
                )
                remaining -= 1
                characters -= len(excerpt)
                cursor = found + len(query)
            complete = body.find(query, cursor) < 0 and not any(
                m["excerpt_truncated"] for m in matches
            )
            item = {
                "uri": uri,
                "matches": matches,
                "complete": complete,
                "coordinate_space": "captured_source" if path else "artifact",
            }
            if path:
                item["source_path"] = path
            results.append(item)
            fingerprints.append([ref.content_hash, query, matches, complete])
        result = ToolResult(
            status="ok",
            content={
                "observation_type": "scoped-artifact-search/v1",
                "query": query,
                "results": results,
                "complete": all(r["complete"] for r in results),
            },
            progress_observation_digest="sha256:"
            + hashlib.sha256(
                json.dumps(fingerprints, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            if fingerprints
            else None,
        )

        return budget.select(result) if budget is not None else result
