"""Reserved ArtifactRead tool backed by the runtime's scoped ArtifactPort."""

from __future__ import annotations

from ksadk.harness.native.artifact_read_contract import (
    ARTIFACT_READ_DEFINITION as ARTIFACT_READ_DEFINITION,
)
from ksadk.harness.native.artifact_read_contract import (
    ARTIFACT_READ_NAME as ARTIFACT_READ_NAME,
)
from ksadk.harness.native.ports import (
    ArtifactPort,
    ArtifactReadRequest,
    ArtifactRef,
    RunConfiguration,
    ToolDefinition,
    ToolPort,
    ToolRequest,
    ToolResult,
)
from ksadk.harness.native.tool_recovery import query_sealed_tool_result

from .native_page_budget import (
    ArtifactPageBudget,
    ArtifactPageCapacityError,
    ArtifactPagePolicy,
    capacity_error_content,
)
from .native_search_budget import ArtifactSearchPolicy, search_definition


class NativeArtifactToolPort:
    def __init__(self, delegate: ToolPort, artifacts: ArtifactPort, *,
                 page_policy: ArtifactPagePolicy | None = None,
                 definitions: tuple[ToolDefinition, ...] = ()) -> None:
        if not callable(getattr(delegate, "execute", None)):
            raise ValueError("artifact tool delegate must implement execute")
        if not callable(getattr(artifacts, "read", None)):
            raise ValueError("artifact port must implement read")
        if page_policy is not None and not isinstance(page_policy, ArtifactPagePolicy):
            raise ValueError("artifact_page_policy_invalid")
        self._delegate, self._artifacts = delegate, artifacts
        self._page_policy, self._definitions = page_policy, definitions
        self._definition = ARTIFACT_READ_DEFINITION.model_copy(update={
            "feedback_profile_digest": page_policy.digest,
        }) if page_policy is not None else ARTIFACT_READ_DEFINITION

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(ARTIFACT_READ_NAME,)
        )

    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(ARTIFACT_READ_NAME,), method="recover_prepared"
        )

    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(ARTIFACT_READ_NAME,), method="resume_prepared"
        )

    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self._delegate, request, owned_names=(ARTIFACT_READ_NAME,), method="resume_checkpoint"
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.call.name != ARTIFACT_READ_NAME:
            result = await self._delegate.execute(request)
            return ToolResult.model_validate_json(result.model_dump_json())
        if request.definition != self._definition:
            return ToolResult(status="error", content={"code": "artifact_definition_mismatch"})
        arguments = request.call.arguments
        if set(arguments).difference({"uri", "offset_chars", "limit_chars"}):
            return ToolResult(status="error", content={"code": "invalid_artifact_read"})
        uri = arguments.get("uri")
        offset = arguments.get("offset_chars", 0)
        limit = arguments.get("limit_chars", 16000)
        if (
            not isinstance(uri, str)
            or not uri
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 32000
        ):
            return ToolResult(status="error", content={"code": "invalid_artifact_read"})
        allowed = {ref.uri: ref for ref in request.available_artifacts}
        ref = allowed.get(uri)
        if ref is None:
            return ToolResult(status="error", content={"code": "artifact_not_available"})
        page_budget = None
        if self._page_policy is not None:
            try:
                page_budget = self._page_policy.resolve(request, self._definitions)
                if not isinstance(page_budget, ArtifactPageBudget):
                    raise ValueError("artifact_page_budget_invalid")
                empty_page = {
                    "uri": ref.uri, "content": "", "offset_chars": offset,
                    "next_offset_chars": offset, "complete": False,
                    "content_hash": ref.content_hash, "size_bytes": ref.size_bytes,
                    "mime_type": ref.mime_type,
                }
                if page_budget.admit_result is None:
                    # Preserve the original public subclass method contract.
                    if not page_budget.fits(empty_page):
                        raise page_budget.unavailable(empty_page)
                elif not page_budget.fits(empty_page, check_admission=False):
                    # The empty page is only a structural preflight, not an
                    # observed result for complete-request admission.
                    raise page_budget.unavailable(empty_page, check_admission=False)
            except ArtifactPageCapacityError as exc:
                return ToolResult(status="error", content=capacity_error_content(exc))
            except (ValueError, KeyError):
                return ToolResult(
                    status="error", content={"code": "artifact_page_budget_unavailable"}
                )
        try:
            page = await self._artifacts.read(
                ArtifactReadRequest(
                    scope=request.scope,
                    run_id=request.run_id,
                    ref=ArtifactRef.model_validate(ref),
                    offset_chars=offset,
                    limit_chars=limit,
                )
            )
        except Exception:
            return ToolResult(status="error", content={"code": "artifact_read_failed"})
        if page.ref != ref or page.offset_chars != offset or len(page.content) > limit:
            return ToolResult(status="error", content={"code": "artifact_read_mismatch"})
        content = {
                "uri": ref.uri,
                "content": page.content,
                "offset_chars": page.offset_chars,
                "next_offset_chars": page.next_offset_chars,
                "complete": page.complete,
                "content_hash": ref.content_hash,
                "size_bytes": ref.size_bytes,
                "mime_type": ref.mime_type,
            }
        if page_budget is not None:
            try:
                content = page_budget.select(content)
            except ArtifactPageCapacityError as exc:
                return ToolResult(status="error", content=capacity_error_content(exc))
            except ValueError:
                return ToolResult(
                    status="error", content={"code": "artifact_page_budget_unavailable"}
                )
        return ToolResult(status="ok", content=content)


def install_artifact_tool(
    configuration: RunConfiguration,
    tools: ToolPort,
    artifacts: ArtifactPort | None,
    *,
    enable_search: bool = False,
    enable_catalog: bool = False,
    page_policy: ArtifactPagePolicy | None = None,
    search_policy: ArtifactSearchPolicy | None = None,
) -> tuple[RunConfiguration, ToolPort]:
    if page_policy is not None and not isinstance(page_policy, ArtifactPagePolicy):
        raise ValueError("artifact_page_policy_invalid")
    if type(enable_catalog) is not bool:
        raise ValueError("artifact_catalog_policy_invalid")
    if search_policy is not None and (
        not isinstance(search_policy, ArtifactSearchPolicy) or not enable_search
    ):
        raise ValueError("artifact_search_policy_invalid")
    if artifacts is None:
        return configuration, tools
    definition = ARTIFACT_READ_DEFINITION.model_copy(update={
        "feedback_profile_digest": page_policy.digest,
    }) if page_policy is not None else ARTIFACT_READ_DEFINITION
    existing = next(
        (tool for tool in configuration.tools if tool.name == ARTIFACT_READ_NAME), None
    )
    if existing is not None and existing != definition:
        raise ValueError("reserved_artifact_tool_conflict")
    definitions = (
        configuration.tools
        if existing is not None
        else (*configuration.tools, definition)
    )
    if enable_catalog:
        from ksadk.harness.native.artifact_catalog_contract import ARTIFACT_LIST_DEFINITION

        from .native_catalog import NativeArtifactCatalogPort

        catalog = next((t for t in definitions if t.name == ARTIFACT_LIST_DEFINITION.name), None)
        if catalog is not None and catalog != ARTIFACT_LIST_DEFINITION:
            raise ValueError("reserved_artifact_tool_conflict")
        if catalog is None:
            definitions = (*definitions, ARTIFACT_LIST_DEFINITION)
        tools = NativeArtifactCatalogPort(
            tools, max_result_bytes=configuration.limits.max_tool_result_bytes
        )
    if enable_search:
        from .native_search import ARTIFACT_SEARCH_DEFINITION, NativeArtifactSearchPort

        search_expected = search_definition(search_policy)
        search = next((t for t in definitions if t.name == ARTIFACT_SEARCH_DEFINITION.name), None)
        if search is not None and search != search_expected:
            raise ValueError("reserved_artifact_tool_conflict")
        if search is None:
            definitions = (*definitions, search_expected)
    configuration = configuration.model_copy(update={"tools": definitions})
    if page_policy is not None and page_policy.bind_configuration is not None:
        bound = page_policy.bind_configuration(configuration)
        if (not isinstance(bound, ArtifactPagePolicy) or bound.digest != page_policy.digest
                or bound.bind_configuration is not None):
            raise ValueError("artifact_page_configuration_binding_invalid")
        page_policy = bound
    if search_policy is not None and search_policy.bind_configuration is not None:
        bound = search_policy.bind_configuration(configuration)
        if (not isinstance(bound, ArtifactSearchPolicy) or bound.digest != search_policy.digest
                or bound.bind_configuration is not None):
            raise ValueError("artifact_search_configuration_binding_invalid")
        search_policy = bound
    if enable_search:
        tools = NativeArtifactSearchPort(
            tools, artifacts, search_policy=search_policy, definitions=definitions,
        )
    return (
        configuration,
        NativeArtifactToolPort(tools, artifacts, page_policy=page_policy, definitions=definitions),
    )


__all__ = [
    "ARTIFACT_READ_DEFINITION",
    "ARTIFACT_READ_NAME",
    "NativeArtifactToolPort",
    "install_artifact_tool",
]
