"""Project Build-locked Studio tools into the Native ToolPort boundary.

The Build owns the allowlist and source hashes. Studio owns authorization;
Native validates the frozen schema and scope on every call. The existing
provider executor still supplies process isolation and workspace scoping.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from ksadk.harness.native.adapters import StrictToolExecutor, ToolAuthorizer, ToolBinding
from ksadk.harness.native.model import ModelImageContent
from ksadk.harness.native.ports import ToolDefinition, ToolPort, ToolRequest, ToolResult
from ksadk.harness.native.state import RunScope
from ksadk.harness.tool_display import builtin_tool_display
from ksadk.harness.stores.native_contract import RunStore
from ksadk.plugins.providers.harness_tools import (
    LOCKED_PYTHON_EXECUTION_POLICY_DIGEST,
    assemble_python_tools,
)
from ksadk.runtime_context import tool_execution_scope
from ksadk.studio.native_browser_port import browser_inspect_parameters
from ksadk.studio.native_command_policy import StudioCommandTimeoutPolicy
from ksadk.studio.native_final_shape import exact_byte_difference, requested_exact_workspace_file
from ksadk.studio.native_language_diagnostics import StudioNativeLanguageDiagnostics
from ksadk.studio.native_read_budget import StudioSourceReadPolicy
from ksadk.studio.native_workspace_diagnostics import (
    WORKSPACE_WRITERS,
    feedback_profile_digest,
    observe_workspace_writes,
)
from ksadk.toolsets.workspace_identity import identity_workspace_root
from ksadk.toolsets.workspace_model_observation import source_model_reply
from ksadk.toolsets.workspace_read_budget import source_read_budget_scope

_MAX_BROWSER_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_MODEL_IMAGE_BASE64_CHARS = 96 * 1024
_EXACT_CONTENT_POLICY_DIGEST = (
    "sha256:" + hashlib.sha256(b"studio.current-goal-write-rejection/v1").hexdigest()
)
_PARALLEL_SAFE_WORKSPACE_READ_TOOLS = frozenset(
    {"workspace_status", "list_workspace_files", "read_workspace_file", "search_workspace_files"}
)


def _prepare_browser_image(payload: bytes) -> tuple[bytes, str] | None:
    """Keep rich screenshots inside the model's explicit image budget.

    Like ZCode's image adapter, the host bounds the input and prepares an
    image for the model before projection. The original screenshot remains on
    disk; only this bounded copy is sent through ModelGateway.
    """
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif payload.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        return None
    encoded_length = ((len(payload) + 2) // 3) * 4
    if encoded_length <= _MAX_MODEL_IMAGE_BASE64_CHARS:
        return payload, mime
    try:
        from PIL import Image

        with Image.open(BytesIO(payload)) as source:
            if source.width * source.height > 16_000_000:
                return None
            for dimension in (1600, 1280, 1024, 800, 640, 512):
                prepared = source.copy()
                prepared.thumbnail((dimension, dimension))
                if prepared.mode != "RGB":
                    rgba = prepared.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    prepared = background
                for quality in (85, 75, 65):
                    output = BytesIO()
                    prepared.save(output, format="JPEG", quality=quality, optimize=True)
                    candidate = output.getvalue()
                    if ((len(candidate) + 2) // 3) * 4 <= _MAX_MODEL_IMAGE_BASE64_CHARS:
                        return candidate, "image/jpeg"
    except (OSError, ValueError):
        return None
    return None


def _browser_image_block(value: object, *, root: Path) -> ModelImageContent | None:
    """Read only a bounded screenshot created under this activation's workspace."""
    # A rendered page may still fail console/action checks; that screenshot is
    # useful repair evidence. The builtin only returns a path after capture.
    if not isinstance(value, Mapping) or type(value.get("ok")) is not bool:
        return None
    relative = value.get("screenshot_path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    try:
        canonical_root = root.resolve(strict=True)
        target = (canonical_root / relative).resolve(strict=True)
        if not target.is_relative_to(canonical_root) or not target.is_file():
            return None
        if target.stat().st_size > _MAX_BROWSER_IMAGE_BYTES:
            return None
        with target.open("rb") as source:
            payload = source.read(_MAX_BROWSER_IMAGE_BYTES + 1)
    except (OSError, RuntimeError, ValueError):
        return None
    if len(payload) > _MAX_BROWSER_IMAGE_BYTES:
        return None
    prepared = _prepare_browser_image(payload)
    if prepared is None:
        return None
    payload, mime = prepared
    return ModelImageContent(
        image_url={"url": f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"}
    )


@dataclass(frozen=True)
class NativeLockedToolSurface:
    definitions: tuple[ToolDefinition, ...]
    executor: ToolPort
    approval_required: frozenset[str]
    command_binding: ToolBinding | None = None
    child_file_tool_names: frozenset[str] = frozenset()


def build_native_locked_tool_surface(
    *,
    bundle_root: Path,
    resolved_agent_spec: Mapping[str, Any],
    workspace_root: Path,
    scope: RunScope,
    store: RunStore,
    authorize: ToolAuthorizer,
    image_input_mode: str = "text_only",
    persistent_browser: ToolPort | None = None,
    effect_timeout_seconds: float = 120.0,
    mcp_server_names: frozenset[str] = frozenset(),
    source_read_policy: StudioSourceReadPolicy | None = None,
    language_diagnostics: StudioNativeLanguageDiagnostics | None = None,
) -> NativeLockedToolSurface:
    """Bind only tools admitted by an immutable Studio Build.

    The caller must provide an authenticated, activation-scoped authorizer.
    An absent authorizer or Agent identity mismatch fails before model use.
    """
    if not isinstance(store, RunStore) or not callable(authorize):
        raise ValueError("native_tool_host_authorization_unavailable")
    if str(resolved_agent_spec.get("agentId") or "") != scope.agent_id:
        raise ValueError("native_tool_agent_scope_mismatch")
    if image_input_mode not in {"native", "text_only"}:
        raise ValueError("native_tool_image_input_mode_invalid")
    if effect_timeout_seconds <= 30:
        raise ValueError("native_tool_effect_timeout_too_short")
    tools, approvals = assemble_python_tools(
        bundle_root,
        dict(resolved_agent_spec),
        workspace_root=workspace_root,
        mcp_server_names=mcp_server_names,
    )

    def run_session(request: ToolRequest) -> str | None:
        try:
            snapshot = store.load(scope, request.run_id)
        except Exception:
            return None
        if snapshot.run_id != request.run_id or snapshot.scope != scope:
            return None
        return snapshot.session_id

    async def checked_authorize(request: ToolRequest):
        # Do not create an approval request for a run the locked Store cannot
        # attribute to this scope and session.
        if run_session(request) is None:
            return "deny"
        return await authorize(request)

    bindings: list[ToolBinding] = []
    for tool in tools.values():
        bounded_command = (
            tool.name == "run_command" and getattr(tool, "source", None) == "sdk-builtin"
        )
        contract_timeout = 20
        if bounded_command:
            for contract in resolved_agent_spec.get("capabilities", {}).get("tools", ()):
                if isinstance(contract, Mapping) and contract.get("name") == tool.name:
                    value = contract.get("timeoutSeconds", 20)
                    if type(value) is int and value > 0:
                        contract_timeout = value
                    break
        command_timeout_cap = max(1, min(int(effect_timeout_seconds) - 30, contract_timeout - 5))
        command_timeout_default = min(120, command_timeout_cap)
        command_policy = (
            StudioCommandTimeoutPolicy(
                tool.parameters, default=command_timeout_default, maximum=command_timeout_cap
            ) if bounded_command else None
        )
        description = tool.description
        if tool.name == "browser_inspect_workspace" and persistent_browser is not None:
            description += (
                " In Native Studio, the same Run and path keep one browser page across "
                "tool calls. Actions use type: click/fill target exactly one element_ref "
                "from controls or CSS selector (not x/y); prefer element_ref for observed "
                "controls so DOM reordering cannot change the target. Press requires key "
                "and optionally one element_ref or selector; fill also requires string value. "
                "Move uses x/y, "
                "scroll uses signed delta_x/delta_y pixels with an optional x/y viewport point, "
                "wait uses ms, and reload has only type. Stale refs return a fresh observation; "
                "choose a current control rather than repeating the stale ref. "
                "Use reload after editing page files; a new Run starts with an isolated page. "
                "Browser success is not task acceptance."
            )
        if bounded_command:
            description += (
                f" Commands default to a {command_timeout_default}s timeout in Native Studio; "
                f"the maximum is {command_timeout_cap}s so the command can finish before "
                "the outer Run deadline. Explicit values must be positive integers within "
                "that maximum; invalid values are rejected before execution. "
                "A timed-out command reports its exit status."
            )
        trusted_read = (
            tool.name in _PARALLEL_SAFE_WORKSPACE_READ_TOOLS
            and getattr(tool, "source", None) == "sdk-builtin"
        )
        concurrent_safe = trusted_read and tool.name not in approvals
        post_write_feedback = (
            tool.name in WORKSPACE_WRITERS and getattr(tool, "source", None) == "sdk-builtin"
        )
        source_reader = trusted_read and tool.name == "read_workspace_file"
        definition = ToolDefinition(
            name=tool.name,
            description=description,
            parameters=(
                browser_inspect_parameters()
                if tool.name == "browser_inspect_workspace" and persistent_browser is not None
                else command_policy.parameters if command_policy is not None
                else tool.parameters
            ),
            read_only=trusted_read,
            progress_observation=True,
            context_role=(
                "workspace_read"
                if trusted_read and tool.name == "read_workspace_file"
                else "workspace_search"
                if trusted_read and tool.name == "search_workspace_files"
                else "none"
            ),
            concurrent_safe=concurrent_safe,
            feedback_profile_digest=(
                source_read_policy.digest
                if source_reader and source_read_policy is not None
                else language_diagnostics.profile_digest
                if post_write_feedback and language_diagnostics is not None
                else feedback_profile_digest()
                if post_write_feedback
                else None
            ),
            execution_policy_digest=(
                _EXACT_CONTENT_POLICY_DIGEST
                if tool.name == "write_workspace_file"
                and getattr(tool, "source", None) == "sdk-builtin"
                else command_policy.digest if command_policy is not None
                else LOCKED_PYTHON_EXECUTION_POLICY_DIGEST
                if getattr(tool, "source", None) == "locked-python"
                else None
            ),
            result_archive_policy=(
                source_read_policy.archive_model_policy
                if source_reader
                and source_read_policy is not None
                and source_read_policy.retain_requested_source
                else None
            ),
            observation_role=(
                "browser"
                if tool.name == "browser_inspect_workspace" and persistent_browser is not None
                else "none"
            ),
        )

        async def invoke(
            request: ToolRequest,
            *,
            bound_tool=tool,
            command_policy=command_policy,
            post_write_feedback=post_write_feedback,
            source_reader=source_reader,
        ) -> ToolResult:
            session_id = run_session(request)
            if session_id is None:
                # The tool has not run. A missing or foreign Run is a definite
                # authorization failure, not an uncertain external side effect.
                return ToolResult(status="error", content={"code": "run_session_unavailable"})
            if bound_tool.name == "browser_inspect_workspace" and persistent_browser is not None:
                # This branch remains inside StrictToolExecutor's locked
                # definition, scope, argument and approval checks. The browser
                # owner retains a page only for this authenticated Run.
                result = await persistent_browser.execute(request)
                blocks = ()
                if image_input_mode == "native":
                    from ksadk.harness.execution_policy import current_execution_policy

                    policy = current_execution_policy()
                    image_root = (
                        policy.workspace_root
                        if policy is not None and policy.workspace_root is not None
                        else identity_workspace_root(
                            workspace_root / ".harness-tools" / "workspace"
                        )
                    )
                    image = _browser_image_block(result.content, root=image_root)
                    if image is not None:
                        blocks = (image,)
                result = result.model_copy(update={"content_blocks": blocks})
                return result
            with tool_execution_scope(
                session_id=session_id,
                run_id=request.run_id,
                invocation_id=request.operation_key,
            ):
                arguments = dict(request.call.arguments)
                if (
                    bound_tool.name == "write_workspace_file"
                    and getattr(bound_tool, "source", None) == "sdk-builtin"
                ):
                    try:
                        run = store.load(scope, request.run_id)
                    except Exception:
                        run = None
                    requirement = (
                        requested_exact_workspace_file(run.goal) if run is not None else None
                    )
                    proposed = arguments.get("content")
                    if (
                        requirement is not None
                        and arguments.get("path") == requirement.relative_path
                        and isinstance(proposed, str)
                        and proposed != requirement.content
                    ):
                        # The approval and schema belong to the ledger call,
                        # not to replacement arguments invented after approval.
                        # Reject before any effect; a new corrected proposal
                        # must pass the same normal admission boundary.
                        return ToolResult(status="error", content={
                            "code": "workspace_content_contract_mismatch",
                            "source": "current_run_goal",
                            "path": requirement.relative_path,
                            "difference": exact_byte_difference(
                                requirement.content.encode("utf-8"), proposed.encode("utf-8")
                            ),
                            "instruction": (
                                "Submit a new write_workspace_file call with the exact UTF-8 "
                                "content requested in the current goal. No file was written."
                            ),
                        })
                effective_timeout = None
                if command_policy is not None:
                    arguments = command_policy.prepare(arguments)
                    if arguments is None:
                        return ToolResult(status="error", content={
                            "code": "invalid_arguments",
                            "instruction": "Submit arguments allowed by the locked command schema.",
                        })
                    effective_timeout = arguments["timeout"]
                read_budget = None
                if source_reader and source_read_policy is not None:
                    try:
                        read_budget = source_read_policy.for_call(
                            store.load(scope, request.run_id),
                            request.call.call_id,
                            tuple(binding.definition for binding in bindings),
                        )
                    except ValueError:
                        return ToolResult(
                            status="error", content={"code": "source_read_budget_unavailable"}
                        )
                with source_read_budget_scope(read_budget):
                    value = await bound_tool.call(
                        arguments,
                        call_id=request.call.call_id,
                    )
            if effective_timeout is not None and isinstance(value, Mapping):
                value = {
                    **value,
                    "effective_timeout_seconds": effective_timeout,
                    "timeout_capped": False,
                }
            # SDK builtin tools report application failures as {"ok": false}.
            # Preserve that distinction in Native's tool receipt so a failed
            # read cannot be counted as a successful observation.
            failed = isinstance(value, Mapping) and value.get("ok") is False
            changed_paths: tuple[str, ...] = ()
            if not failed:
                paths: list[object] = []
                if bound_tool.name in {
                    "write_workspace_file",
                    "edit_workspace_file",
                    "multi_edit_workspace_file",
                }:
                    paths = [request.call.arguments.get("path")]
                elif bound_tool.name == "write_workspace_files" and isinstance(value, Mapping):
                    written = value.get("written")
                    if isinstance(written, list):
                        paths = [item.get("path") for item in written if isinstance(item, Mapping)]
                normalized_paths = []
                for path in paths:
                    if not isinstance(path, str) or not path or "\\" in path:
                        continue
                    normalized = PurePosixPath(path)
                    if not normalized.is_absolute() and ".." not in normalized.parts:
                        normalized_paths.append(normalized.as_posix())
                changed_paths = tuple(dict.fromkeys(normalized_paths))
            blocks = ()
            if post_write_feedback and not failed and changed_paths:
                from ksadk.harness.execution_policy import current_execution_policy

                policy = current_execution_policy()
                diagnostic_root = (
                    policy.workspace_root
                    if policy is not None and policy.workspace_root is not None
                    else identity_workspace_root(workspace_root / ".harness-tools" / "workspace")
                )
                blocks = (observe_workspace_writes(root=diagnostic_root, paths=changed_paths),)
                if language_diagnostics is not None:
                    blocks += (await language_diagnostics.observe(
                        root=diagnostic_root, paths=changed_paths,
                    ),)
            if image_input_mode == "native" and bound_tool.name == "browser_inspect_workspace":
                from ksadk.harness.execution_policy import current_execution_policy

                policy = current_execution_policy()
                image_root = (
                    policy.workspace_root
                    if policy is not None and policy.workspace_root is not None
                    else identity_workspace_root(workspace_root / ".harness-tools" / "workspace")
                )
                image = _browser_image_block(value, root=image_root)
                if image is not None:
                    blocks = (image,)
            return ToolResult(
                status="error" if failed else "ok",
                public_display=(
                    builtin_tool_display(bound_tool.name, request.call.arguments, value)
                    if getattr(bound_tool, "source", None) == "sdk-builtin" else None
                ),
                content=value,
                model_content=(
                    source_model_reply(value)
                    if source_reader and source_read_policy is not None and not failed
                    else None
                ),
                content_blocks=blocks,
                workspace_changed_paths=changed_paths,
            )

        bindings.append(ToolBinding(definition, invoke))
    return NativeLockedToolSurface(
        definitions=tuple(binding.definition for binding in bindings),
        executor=StrictToolExecutor(scope=scope, tools=bindings, authorize=checked_authorize),
        approval_required=frozenset(approvals),
        command_binding=next(
            (binding for binding in bindings if binding.definition.name == "run_command"), None
        ),
        child_file_tool_names=frozenset(
            tool.name for tool in tools.values()
            if getattr(tool, "source", None) == "sdk-builtin"
            and tool.name in (_PARALLEL_SAFE_WORKSPACE_READ_TOOLS | WORKSPACE_WRITERS
                              | {"delete_workspace_file"})
        ),
    )
