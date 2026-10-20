"""Page only the runtime-provided authorized artifact inventory; no store scan."""

from __future__ import annotations

import hashlib
import json

from ksadk.harness.native.artifact_catalog_contract import (
    ARTIFACT_LIST_DEFINITION,
    ARTIFACT_LIST_NAME,
)
from ksadk.harness.native.ports import ToolPort, ToolRequest, ToolResult
from ksadk.harness.native.tool_recovery import query_sealed_tool_result


class NativeArtifactCatalogPort:
    def __init__(self, delegate: ToolPort, *, max_result_bytes: int = 65536):
        if type(max_result_bytes) is not int or max_result_bytes < 1024:
            raise ValueError("artifact_catalog_budget_invalid")
        self._delegate = delegate
        self._max_result_bytes = max_result_bytes

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(ARTIFACT_LIST_NAME,)
        )

    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(ARTIFACT_LIST_NAME,), method="recover_prepared"
        )

    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(ARTIFACT_LIST_NAME,), method="resume_prepared"
        )

    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(ARTIFACT_LIST_NAME,), method="resume_checkpoint"
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.call.name != ARTIFACT_LIST_NAME:
            result = await self._delegate.execute(request)
            return result.model_copy(deep=True)
        if request.definition != ARTIFACT_LIST_DEFINITION:
            return ToolResult(status="error", content={"code": "artifact_definition_mismatch"})
        args = request.call.arguments
        offset, limit = args.get("offset", 0), args.get("limit", 8)
        if (
            set(args).difference({"offset", "limit"})
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 8
        ):
            return ToolResult(status="error", content={"code": "invalid_artifact_list"})
        refs = request.available_artifacts
        end = min(offset + limit, len(refs))
        inventory_digest = hashlib.sha256(
            json.dumps(
                [ref.uri for ref in refs], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        while True:
            content = {
                "artifacts": [ref.model_dump(mode="json") for ref in refs[offset:end]],
                "offset": offset,
                "next_offset": end if end < len(refs) else None,
                "total": len(refs),
                "inventory_digest": inventory_digest,
                "authority": "locators_only",
            }
            # Use the same full text payload envelope as result governance.
            # Catalog pages must not archive themselves and grow the inventory
            # merely because the caller requested eight locators at a small cap.
            payload = {"status": "ok", "content": content, "content_blocks": []}
            if (
                len(
                    json.dumps(
                        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode()
                )
                <= self._max_result_bytes
            ):
                return ToolResult(status="ok", content=content)
            if end <= offset + 1:
                return ToolResult(
                    status="error", content={"code": "artifact_catalog_entry_over_budget"}
                )
            end -= 1
