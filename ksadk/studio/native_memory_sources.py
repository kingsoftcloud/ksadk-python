"""Confirm eligible user inputs against their canonical Run owners.

History imports retain original identities. A lifecycle boundary or a repeated
projection is not another observation; tool/context/model prose is ineligible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from ksadk.harness.native.user_input import user_content_text
from ksadk.memory.extraction import native_user_memory_candidate
from ksadk.plugins.memory_provider import (
    MemoryPrincipal,
    MemoryUserObservation,
    memory_scope_bindings,
)


def memory_bundle_snapshot(context):
    """Freeze the same declared Build used by recall, tools and lifecycle consumers."""
    from ksadk.harness.native.hook_runtime_contract import _json_bytes

    def json_value(value):
        if isinstance(value, Mapping):
            return {key: json_value(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_value(child) for child in value]
        return value

    bundle = context.bundle
    return _json_bytes({
        "manifest": bundle.manifest.model_dump(mode="json"),
        "profile": bundle.composition.profile.model_dump(mode="json"),
        "lock": bundle.composition.plugin_lock.model_dump(mode="json"),
        "resolved_spec": json_value(bundle.resolved_agent_spec),
    })


def original_user_memory_sources(store, scope, run_id, indexed_messages):
    principal = MemoryPrincipal(**scope.model_dump())
    business_scope, scope_id = memory_scope_bindings(principal, ("user",))[0]
    originals, observations, seen = {}, [], set()
    unavailable = 0
    for index, message in indexed_messages:
        if message.get("role") != "user" or message.get("kind") is not None:
            continue
        body = user_content_text(message.get("content"))
        if body is None:
            continue
        source_run = message.get("source_run_id", run_id)
        source_index = message.get("source_message_index", index)
        ref = f"native-message://{source_run}#message={source_index}"
        if native_user_memory_candidate(
            body, source_event_id=ref, scope=business_scope, scope_id=scope_id,
        ) is None:
            continue
        if source_run not in originals:
            try:
                originals[source_run] = store.load(scope, source_run)
            except KeyError:
                originals[source_run] = None
        origin = originals[source_run]
        if origin is None:
            unavailable += 1
            continue
        if type(source_index) is not int or not 0 <= source_index < len(origin.messages):
            raise ValueError("native_memory_original_source_unbound")
        original = origin.messages[source_index]
        if (original.get("role") != "user" or original.get("kind") is not None
                or original.get("content") != message["content"]):
            raise ValueError("native_memory_original_source_unbound")
        if ref not in seen:
            seen.add(ref)
            observations.append(MemoryUserObservation(
                source_event_id=ref, source_session_id=origin.session_id, content=body,
                source_digest="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            ))
    return tuple(observations), unavailable
