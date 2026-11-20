"""Bind Python Hook producers and host authorization to independent Runs."""

from __future__ import annotations

import hashlib
import json
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from ksadk.harness.native.hook_executor import HookAuthorizer, HookBinding, ScopedHookExecutor
from ksadk.harness.native.hook_runtime_contract import HookInvocationRequest, PreCompactSubject
from ksadk.harness.native.model import ModelToolCall
from ksadk.harness.service_runtime import ServiceHook
from ksadk.plugins.providers.harness_hooks import hook_configuration
from ksadk.runtime import RunHandle
from ksadk.studio.native_hook_registration import validate_hook_authorization_policy
from ksadk.studio.native_scope import native_scope_from_activation

if TYPE_CHECKING:
    from ksadk.harness.native.hook_runtime_contract import HookDefinition
    from ksadk.plugins.providers.harness import HarnessEngineActivationContext


def _tool_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    usage = result.get("resourceUsage") or {}
    counters = {
        "modelCalls": "model_calls", "toolCalls": "tool_calls",
        "inputTokens": "input_tokens", "outputTokens": "output_tokens",
        "cachedInputTokens": "cached_input_tokens", "reasoningTokens": "reasoning_tokens",
    }
    artifacts = []
    for item in result.get("artifactRefs", []):
        digest = item["digest"]
        artifacts.append({
            "uri": item["uri"], "content_hash": digest if ":" in digest else "sha256:" + digest,
            "mime_type": item["mediaType"], "size_bytes": item["sizeBytes"],
        })
    return {
        "status": result["status"], "content": result["content"],
        "workspace_changed_paths": result.get("workspaceChangedPaths") or [],
        "artifact_refs": artifacts,
        "resource_usage": {
            **{target: usage[key] for key, target in counters.items() if key in usage},
            "child_run_ids": result.get("childRunIds") or [],
        },
    }


def _compaction(value: dict[str, Any] | None) -> PreCompactSubject | None:
    if value is None:
        return None
    return PreCompactSubject.model_validate({
        "source_revision": value["sourceRevision"],
        "source_context_digest": value["sourceContextDigest"],
        "source_message_count": value["sourceMessageCount"],
        "message_refs": value["messageRefs"], "messages": value["messages"],
        "retained_message_refs": value["retainedMessageRefs"],
        **({"source_projection": "canonical_user"}
           if value.get("sourceProjection") == "canonical_user" else {}),
    })


def service_hooks(
    context: HarnessEngineActivationContext,
    cleanup: AsyncExitStack,
    *,
    authorize: HookAuthorizer | None,
    authorization_version: str | None,
) -> tuple[tuple[ServiceHook, ...], int | None]:
    if not context.hook_sources:
        return (), None
    scope = native_scope_from_activation(context)
    validate_hook_authorization_policy(authorize, authorization_version)
    if context.activation_available is None or not context.activation_generation:
        raise ValueError("Harness hooks require an owned PluginHost activation")
    bundle = context.bundle
    manifest_json = bundle.manifest.model_dump_json()
    profile_json = bundle.composition.profile.model_dump_json()
    lock_json = bundle.composition.plugin_lock.model_dump_json()

    def available() -> bool:
        return (
            context.activation_available() is True
            and bundle.manifest.model_dump_json() == manifest_json
            and bundle.composition.profile.model_dump_json() == profile_json
            and bundle.composition.plugin_lock.model_dump_json() == lock_json
        )

    async def admitted(request: HookInvocationRequest) -> str:
        if authorize is None or not available():
            return "deny"
        return await authorize(request)

    bindings = []
    stop_limits = set()
    for source in context.hook_sources:
        config = hook_configuration(bundle, source)
        ref = f"plugin://{source.plugin_id}@{source.plugin_version}"
        identity = json.dumps([
            bundle.bundle_digest, context.profile_digest, context.plugin_lock_digest,
            ref, source.slot, config.model_dump(mode="json"), authorization_version,
            "engine-hooks/v1",
        ], sort_keys=True)
        binding_digest = "sha256:" + hashlib.sha256(identity.encode()).hexdigest()
        definitions = config.definitions(source_ref=ref, binding_digest=binding_digest)
        if config.stop_continuation_limit is not None:
            stop_limits.add(config.stop_continuation_limit)

        async def callback(request, *, read=source.runtime.harness_hook):
            return await read(bundle, request)

        executor = ScopedHookExecutor(
            scope=scope, bindings=tuple(HookBinding(item, callback, available)
                                       for item in definitions),
            authorize=admitted if authorize is not None else None,
            cleanup_timeout_seconds=config.cleanup_timeout_seconds,
        )
        cleanup.push_async_callback(executor.aclose)

        for item in definitions:
            async def invoke(
                handle: RunHandle, params: dict[str, Any],
                *, hook: HookDefinition = item, owner: ScopedHookExecutor = executor,
            ) -> dict[str, Any]:
                if handle.session_id != context.activation_key:
                    raise ValueError("Harness Hook session differs from activation")
                payload = params["input"]
                call, candidate = payload["call"], payload["candidate"]
                request = HookInvocationRequest(
                    scope=scope, run_id=handle.run_id, session_id=handle.session_id,
                    turn_id=params["turnId"], operation_key=params["operationKey"],
                    goal_revision=params["goalRevision"], goal=payload["goal"],
                    tool_call_count=payload["toolCallCount"],
                    stop_continuation_count=payload["stopContinuationCount"],
                    event=params["event"], definition=hook,
                    call=ModelToolCall(
                        call_id=call["callId"], name=call["name"], arguments=call["arguments"],
                    ) if call else None,
                    tool_result=_tool_result(payload["toolResult"]),
                    candidate_id=candidate["candidateId"] if candidate else None,
                    candidate_text=candidate["text"] if candidate else None,
                    compaction=_compaction(payload["compaction"]),
                )
                result = await owner.invoke(request)
                output = result.output
                return {
                    "status": result.status, "reason": result.reason,
                    "output": {"action": output.action,
                               "additionalContext": list(output.additional_context),
                               "updatedArguments": output.updated_arguments} if output else None,
                }

            bindings.append(ServiceHook({
                "hookId": item.hook_id, "sourceRef": item.source_ref,
                "bindingDigest": item.binding_digest,
                "executionPolicyDigest": item.execution_policy_digest,
                "readOnly": item.read_only, "events": list(item.events),
                "timeoutMs": item.timeout_seconds * 1000, "maxInputBytes": item.max_input_bytes,
                "maxOutputBytes": item.max_output_bytes, "failureMode": item.failure_mode,
                **({"toolNames": list(item.tool_names)} if item.tool_names else {}),
                **({"compactionSourceProjection": item.compaction_source_projection}
                   if item.compaction_source_projection else {}),
            }, invoke))
    if len(stop_limits) > 1:
        raise ValueError("Conflicting Harness Stop continuation limits")
    return tuple(bindings), next(iter(stop_limits), None)
