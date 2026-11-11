"""Project Build-locked Studio tools into the Native ToolPort boundary.

The Build owns the allowlist and source hashes. Studio owns authorization;
Native validates the frozen schema and scope on every call. The existing
provider executor still supplies process isolation and workspace scoping.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from ksadk.harness.native.adapters import StrictToolExecutor, ToolAuthorizer, ToolBinding
from ksadk.harness.native.model import ModelImageContent
from ksadk.harness.native.ports import RunLimits, ToolDefinition, ToolPort, ToolRequest, ToolResult
from ksadk.harness.native.state import RunScope
from ksadk.harness.stores.native_contract import RunStore
from ksadk.harness.tool_display import builtin_tool_display
from ksadk.plugins.providers.harness_tools import (
    LOCKED_PYTHON_EXECUTION_POLICY_DIGEST,
    assemble_python_tools,
)
from ksadk.runtime_context import tool_execution_scope
from ksadk.studio.native_background_locator import BackgroundCommandLocator
from ksadk.studio.native_browser_port import browser_inspect_parameters
from ksadk.studio.native_command_policy import (
    StudioBackgroundWaitPolicy,
    StudioCommandTimeoutPolicy,
)
from ksadk.studio.native_directory_observation import (
    DIRECTORY_MODEL_DESCRIPTION,
    DIRECTORY_SCOPE_MODEL_DESCRIPTION,
    directory_feedback_digest,
    directory_model_reply,
)
from ksadk.studio.native_final_shape import exact_byte_difference, requested_exact_workspace_file
from ksadk.studio.native_language_diagnostics import StudioNativeLanguageDiagnostics
from ksadk.studio.native_read_budget import StudioSourceReadPolicy
from ksadk.studio.native_workspace_diagnostics import (
    WORKSPACE_WRITERS,
    feedback_profile_digest,
    observe_workspace_writes,
)
from ksadk.tools.artifact_observation import OBSERVED_ARCHIVE_MODEL_POLICY
from ksadk.toolsets.sandbox import background_backend_scope, background_declaration_scope
from ksadk.toolsets.workspace_identity import identity_workspace_root
from ksadk.toolsets.workspace_model_observation import source_model_reply
from ksadk.toolsets.workspace_read_budget import source_read_budget_scope

_MAX_BROWSER_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_MODEL_IMAGE_BASE64_CHARS = 96 * 1024
_EXACT_CONTENT_POLICY_DIGEST = (
    "sha256:" + hashlib.sha256(b"studio.current-goal-write-rejection/v1").hexdigest()
)
_DELIVERY_EDIT_POLICY_DIGEST = (
    "sha256:" + hashlib.sha256(b"studio.exact-delivery-edit/v1").hexdigest()
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
    background_declaration: Any | None = None,
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
    execution = resolved_agent_spec.get("execution") or {}
    directory_policy = execution.get(
        "nativeDirectoryObservationPolicy", execution.get("native_directory_observation_policy"),
    ) or "studio-directory-observation/v1"
    directory_navigation = execution.get(
        "nativeDirectoryNavigationPolicy", execution.get("native_directory_navigation_policy"),
    ) == "receipt-directory-progress/v1"
    wall_policy = execution.get(
        "nativeWallDeliveryPolicy", execution.get("native_wall_delivery_policy"),
    )
    wall_delivery = wall_policy in {
        "deadline-delivery/v1", "deadline-delivery/v2", "deadline-delivery/v3",
        "deadline-delivery/v4", "deadline-delivery/v5", "deadline-delivery/v6", "deadline-delivery/v7",
    }
    delivery_editor = (
        wall_policy in {"deadline-delivery/v5", "deadline-delivery/v6", "deadline-delivery/v7"}
        and getattr(tools.get("edit_workspace_file"), "source", None) == "sdk-builtin"
    )
    delivery_self_read = wall_policy in {"deadline-delivery/v6", "deadline-delivery/v7"} and delivery_editor
    lifecycle_enabled = (resolved_agent_spec.get("execution") or {}).get(
        "nativeCommandLifecyclePolicy",
        (resolved_agent_spec.get("execution") or {}).get("native_command_lifecycle_policy"),
    ) == "owned-local-command/v1"
    background_notifications = lifecycle_enabled and (resolved_agent_spec.get("execution") or {}).get(
        "nativeBackgroundNotificationPolicy",
        (resolved_agent_spec.get("execution") or {}).get("native_background_notification_policy"),
    ) == "completed-before-model/v1"
    background_lifecycle_bound = all(
        getattr(tools.get(name), "source", None) == "sdk-builtin"
        for name in ("read_command_output", "stop_command")
    )
    for tool in tools.values():
        bounded_command = (
            tool.name == "run_command" and getattr(tool, "source", None) == "sdk-builtin"
        )
        bounded_wait = (
            tool.name == "read_command_output" and getattr(tool, "source", None) == "sdk-builtin"
        )
        contract_timeout = 20
        if bounded_command or bounded_wait:
            for contract in resolved_agent_spec.get("capabilities", {}).get("tools", ()):
                if isinstance(contract, Mapping) and contract.get("name") == tool.name:
                    value = contract.get("timeoutSeconds", 20)
                    if type(value) is int and value > 0:
                        contract_timeout = value
                    break
        command_timeout_cap = max(1, min(int(effect_timeout_seconds) - 30, contract_timeout - 5))
        command_timeout_default = min(120, command_timeout_cap)
        command_parameters = tool.parameters
        if lifecycle_enabled and bounded_command and not background_lifecycle_bound:
            command_parameters = deepcopy(tool.parameters)
            command_parameters.setdefault("allOf", []).append({
                "properties": {"background": {"const": False}},
            })
        command_policy = (
            StudioCommandTimeoutPolicy(
                command_parameters, default=command_timeout_default, maximum=command_timeout_cap
            ) if bounded_command else None
        )
        wait_policy = (StudioBackgroundWaitPolicy(
            tool.parameters, contract_seconds=contract_timeout, effect_seconds=effect_timeout_seconds,
        ) if bounded_wait and contract_timeout > 20 else None)
        description = tool.description
        if delivery_editor and getattr(tool, "source", None) == "sdk-builtin":
            if tool.name == "write_workspace_file":
                description += (
                    " Keep each call within the model output limit. For a large deliverable, "
                    "write a complete first portion, then use the bound edit_workspace_file "
                    "in later calls to replace a known unique tail or marker with the next "
                    "portion. Wait for each successful receipt; incomplete calls wrote nothing. "
                    "A partial file does not complete the task."
                )
            elif tool.name == "edit_workspace_file":
                description += (
                    " This Build permits exact edits during deadline delivery when this tool "
                    "is admitted. Extend an already successfully written deliverable in bounded "
                    "portions using an exact unique tail or marker; do not resend its whole body. "
                    "Approval, current file revision and replacement checks still apply."
                )
        if wait_policy is not None:
            description += (
                f" This Build permits wait_seconds from 0 to {wait_policy.maximum}. "
                "Use one bounded wait instead of repeatedly polling through the model. "
                "Cancelling a wait does not stop the process or extend its lifetime."
            )
        if lifecycle_enabled and bounded_command and not background_lifecycle_bound:
            description += (
                " Background execution is disabled in this Build. Bind both "
                "read_command_output and stop_command for the complete lifecycle."
            )
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
        directory_reader = trusted_read and tool.name == "list_workspace_files"
        directory_result_bytes = (
            source_read_policy.artifact_result_max_bytes
            if source_read_policy is not None
            and source_read_policy.artifact_result_max_bytes is not None
            else RunLimits().max_tool_result_bytes
        )
        if directory_reader:
            description += (
                DIRECTORY_SCOPE_MODEL_DESCRIPTION
                if directory_policy == "studio-directory-observation/v3"
                else DIRECTORY_MODEL_DESCRIPTION
            )
        definition = ToolDefinition(
            background_command_policy=(
                "owned-command-launch/v1"
                if bounded_command and background_lifecycle_bound and background_notifications
                else None
            ),
            name=tool.name,
            description=description,
            parameters=(
                browser_inspect_parameters()
                if tool.name == "browser_inspect_workspace" and persistent_browser is not None
                else wait_policy.parameters if wait_policy is not None
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
                else "workspace_list"
                if directory_reader and directory_navigation
                else "none"
            ),
            concurrent_safe=concurrent_safe,
            delivery_write=(
                wall_delivery and post_write_feedback and (
                    tool.name == "write_workspace_file"
                    or (delivery_editor and tool.name == "edit_workspace_file")
                )
            ),
            delivery_artifact_role=(
                "read" if delivery_self_read and source_reader
                else "write" if delivery_self_read and post_write_feedback
                and tool.name in {"write_workspace_file", "edit_workspace_file"}
                else "none"
            ),
            feedback_profile_digest=(
                directory_feedback_digest(directory_result_bytes, policy=directory_policy)
                if directory_reader
                else source_read_policy.digest
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
                else _DELIVERY_EDIT_POLICY_DIGEST
                if delivery_editor and tool.name == "edit_workspace_file"
                and getattr(tool, "source", None) == "sdk-builtin"
                else wait_policy.digest if wait_policy is not None
                else command_policy.digest if command_policy is not None
                else LOCKED_PYTHON_EXECUTION_POLICY_DIGEST
                if getattr(tool, "source", None) == "locked-python"
                else None
            ),
            result_archive_policy=(
                OBSERVED_ARCHIVE_MODEL_POLICY
                if directory_reader
                else source_read_policy.archive_model_policy
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
            legacy_wait=bounded_wait and wait_policy is None,
            post_write_feedback=post_write_feedback,
            source_reader=source_reader,
            directory_reader=directory_reader,
            directory_result_bytes=directory_result_bytes,
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
                if delivery_editor and bound_tool.name == "edit_workspace_file":
                    run = store.load(scope, request.run_id)
                    requirement = requested_exact_workspace_file(run.goal)
                    if requirement is not None and arguments.get("path") == requirement.relative_path:
                        return ToolResult(status="error", content={
                            "code": "workspace_exact_content_requires_write",
                            "path": requirement.relative_path,
                            "instruction": (
                                "This goal requires exact file bytes. Submit the complete "
                                "requested content through write_workspace_file; no edit was applied."
                            ),
                        })
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
                if (bound_tool.name == "run_command"
                        and getattr(bound_tool, "source", None) == "sdk-builtin"
                        and arguments.get("background") is True
                        and not (lifecycle_enabled and background_lifecycle_bound)):
                    return ToolResult(status="error", content={
                        "code": "background_lifecycle_unavailable",
                        "instruction": "Build with the owned command lifecycle and both query/stop tools.",
                    })
                if legacy_wait:
                    wait = arguments.get("wait_seconds", 0)
                    if type(wait) is not int or not 0 <= wait <= 10:
                        return ToolResult(status="error", content={"code": "invalid_arguments"})
                effective_timeout = None
                if command_policy is not None:
                    arguments = command_policy.prepare(arguments)
                    if arguments is None:
                        return ToolResult(status="error", content={
                            "code": "invalid_arguments",
                            "instruction": "Submit arguments allowed by the locked command schema.",
                        })
                    if not arguments.get("background"):
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
                    # For read/stop, the backend must be proven from the canonical
                    # launch receipt (owned ref + committed launch backend). If it
                    # cannot be proven, fail closed — do not call any query/kill
                    # factory and do not fall back to the environment.
                    bg_backend = None
                    if (bound_tool.name in {"read_command_output", "stop_command"}
                            and getattr(bound_tool, "source", None) == "sdk-builtin"):
                        bg_backend = _resolve_read_stop_backend(
                            store, scope, request.run_id,
                            arguments.get("task_id"),
                        )
                        if bg_backend is None:
                            return ToolResult(status="error", content={
                                "code": "background_command_backend_unavailable",
                                "instruction": (
                                    "The background command backend could not be "
                                    "proven from the canonical launch receipt."
                                ),
                            })
                    with background_backend_scope(bg_backend):
                        # Bind the host-declared typed E2B connection for this
                        # tool call so launch/read/stop all use the same authority
                        # as the Composer's declaration. A host execution scope,
                        # never derived from model/HTTP arguments.
                        with background_declaration_scope(background_declaration):
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
            unknown_command_effect = (
                getattr(bound_tool, "source", None) == "sdk-builtin"
                and bound_tool.name in {"run_command", "stop_command"}
                and isinstance(value, Mapping)
                and value.get("status") == "unknown"
                and isinstance(value.get("task_id"), str)
            )
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
                status="uncertain" if unknown_command_effect else "error" if failed else "ok",
                background_command_ref=(
                    _issued_background_ref(request, value)
                    if background_notifications and background_lifecycle_bound
                    and getattr(bound_tool, "source", None) == "sdk-builtin"
                    and bound_tool.name == "run_command"
                    and arguments.get("background") is True and not failed
                    and not unknown_command_effect else None
                ),
                background_command_finished=(
                    background_notifications and background_lifecycle_bound
                    and getattr(bound_tool, "source", None) == "sdk-builtin"
                    and bound_tool.name == "run_command"
                    and arguments.get("background") is True and not failed
                    and not unknown_command_effect and isinstance(value, Mapping)
                    and value.get("status") in {
                        "completed", "failed", "cancelled", "timed_out", "output_limit"
                    }
                ),
                public_display=(
                    builtin_tool_display(bound_tool.name, request.call.arguments, value)
                    if getattr(bound_tool, "source", None) == "sdk-builtin" else None
                ),
                content=value,
                model_content=(
                    directory_model_reply(
                        value, arguments, result_bytes=directory_result_bytes, policy=directory_policy,
                    )
                    if directory_reader and not failed
                    else source_model_reply(value)
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


def _issued_background_ref(request, value):
    from ksadk.harness.native.state import BackgroundCommandRef

    if (not isinstance(value, Mapping)
            or value.get("backend") not in {"sandbox/local_process", "sandbox/e2b"}
            or not isinstance(value.get("task_id"), str)):
        return None
    return BackgroundCommandRef(task_id=value["task_id"], source_run_id=request.run_id,
                                operation_key=request.operation_key)


def _resolve_read_stop_backend(store, scope, run_id, task_id):
    """Resolve the host-selected backend for a read/stop call from the canonical
    launch receipt of the task referenced by ``task_id``. Returns None when the
    task is not found or the receipt carries no provable backend (the tool then
    reads the environment, which is the existing local behavior). Never trusts
    model arguments or the request for the backend."""
    if not isinstance(task_id, str) or not task_id:
        return None
    try:
        snapshot = store.load(scope, run_id)
    except Exception:
        return None
    ref = BackgroundCommandLocator._find_ref(snapshot, task_id)
    if ref is None:
        return None
    return BackgroundCommandLocator._receipt_backend(
        store, scope, ref, current_session_id=snapshot.session_id,
    )
