"""Project a locked DSH Memory recall into the existing Native Context effect."""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timezone
from urllib.parse import quote

from ksadk.harness.native.context_contributions import ContextSourceDefinition
from ksadk.harness.native.context_contributor_contract import (
    ContextContributorPolicy,
    contributor_messages,
    utc_now,
)
from ksadk.harness.native.studio_admission import studio_mapping_digest
from ksadk.memory.models import MemoryRecord, MemorySearchResult
from ksadk.memory.policy import content_hash
from ksadk.plugins.context_contributor import (
    ContextContributorCapabilities,
    ContextContributorResponse,
    ContextFragment,
)
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.memory_provider import (
    MemoryPrincipal,
    MemoryRecallRequest,
    memory_scope_bindings,
)
from ksadk.studio.contracts import ContextSpec, MemorySpec


def _time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def admitted_memory_records(result, *, principal, scopes, top_k):
    """One host data admission for recall and explicit search projections."""
    if not isinstance(result, MemorySearchResult) or result.status not in {"ok", "not_configured"}:
        raise PluginHostError("memory_recall_failed", "Memory query did not succeed")
    allowed = set(memory_scope_bindings(principal, scopes))
    now = utc_now()
    for record in result.records[:top_k]:
        if not isinstance(record, MemoryRecord):
            raise ValueError("native_memory_record_invalid")
        if (
            record.tenant_id != principal.tenant_id
            or (record.scope != "org" and record.workspace_id != principal.workspace_id)
            or (record.scope, record.scope_id) not in allowed
        ):
            raise ValueError("native_memory_record_unbound")
        if record.content_hash != content_hash(record.content):
            raise ValueError("native_memory_content_mismatch")
        expiry = [_time(value) for value in (record.expires_at, record.valid_to) if value]
        valid_from = _time(record.valid_from)
        if (
            record.status != "active"
            or record.sensitivity == "high"
            or any(value <= now for value in expiry)
            or (valid_from is not None and valid_from > now)
        ):
            continue
        yield record


class StudioMemoryContextSource:
    def __init__(self, context, scope, planner, binding, policy):
        self._principal = MemoryPrincipal(**scope.model_dump())
        self._policy = policy
        self._planner = planner
        self._read = binding.runtime.memory_recall
        self._provider_ref = f"plugin://{binding.plugin_id}@{binding.plugin_version}"
        grant = ContextContributorPolicy(
            capabilities=ContextContributorCapabilities(
                contributor_id="studio.memory-recall",
                trust_level="untrusted",
                max_tokens=policy.recall.max_tokens,
                timeout_ms=3000,
                # A mutable store can change through this Run's tools or an
                # independent writer. Admit a fresh bounded snapshot before
                # each model request using the existing Context effect.
                cacheability="none",
                failure_mode="skip",
            ),
            policy_ref="policy://studio/native-memory-recall@2",
            allowed_classifications=("internal",),
            workspace_root=str(context.user_workspace_root or context.workspace_root),
            tokenizer_id=planner.tokenizer_id,
        )
        self.definition = ContextSourceDefinition(
            source_id="dsh-memory-" + hashlib.sha256(self._provider_ref.encode()).hexdigest()[:24],
            source_ref=self._provider_ref,
            binding_digest=studio_mapping_digest(
                {
                    "bundle_digest": context.bundle.bundle_digest,
                    "profile_digest": context.profile_digest,
                    "plugin_lock_digest": context.plugin_lock_digest,
                    "source_ref": self._provider_ref,
                    "slot": binding.slot,
                    "memory": policy.model_dump(mode="json"),
                    "scope_namespace": "native-memory-v1",
                    "policy": grant.model_dump(mode="json"),
                }
            ),
            projection="fragment_sections",
            contributor_policy=grant,
            max_chars=131_072,
            failure_mode="skip",
            timeout_seconds=3,
        )

    async def read(self, bundle, request):
        principal = self._principal
        if (
            request.scope.agent_id != principal.agent_id
            or request.scope.actor_id != principal.user_id
            or request.metadata.get("tenant_id") != principal.tenant_id
            or request.metadata.get("workspace_id") != principal.workspace_id
        ):
            raise ValueError("native_memory_scope_unbound")
        budget = min(self._policy.recall.max_tokens, request.remaining_budget)
        if not budget:
            return ContextContributorResponse(
                response_format="ksadk.context-response/v1", fragments=()
            )
        recall = MemoryRecallRequest(
            principal=principal,
            query=request.user_input,
            scopes=tuple(self._policy.scopes),
            top_k=self._policy.recall.top_k,
            max_tokens=budget,
            min_score=self._policy.recall.min_score,
        )
        result = await self._read(bundle, recall)
        fragments = []
        for record in admitted_memory_records(
            result,
            principal=principal,
            scopes=recall.scopes,
            top_k=recall.top_k,
        ):
            expires = [
                value
                for value in (_time(record.expires_at), _time(record.valid_to))
                if value is not None
            ]
            expiry = min(expires) if expires else None
            fragments.append(
                ContextFragment(
                    fragment_format="ksadk.context-fragment/v1",
                    item_id="memory-" + hashlib.sha256(record.memory_id.encode()).hexdigest(),
                    kind="recalled_memory",
                    content=record.content,
                    source_refs=(
                        f"memory://{self._provider_ref.split('://', 1)[1].split('@', 1)[0]}"
                        f"/{quote(record.memory_id, safe='')}",
                    ),
                    classification="internal",
                    expires_at=expiry,
                    token_estimate=self._planner._count_message(
                        {"role": "user", "content": record.content}
                    ),
                    trust_level="untrusted",
                    priority=0,
                    required=False,
                    droppable=True,
                    truncatable=False,
                    stable=False,
                    group_id=None,
                    seq_start=record.source_seq_range[0] if record.source_seq_range else None,
                    seq_end=record.source_seq_range[1] if record.source_seq_range else None,
                    score=None,
                    content_hash=record.content_hash,
                    metadata={
                        "memory_id": record.memory_id,
                        "provider_ref": self._provider_ref,
                        "tenant_id": record.tenant_id,
                        "workspace_id": record.workspace_id,
                        "scope": record.scope,
                        "scope_id": record.scope_id,
                        "version": record.version,
                        "created_at": record.created_at,
                        "updated_at": record.updated_at,
                        "source_session_id": record.source_session_id,
                        "source_event_ids": list(record.source_event_ids),
                        "source_artifact_refs": list(record.source_artifact_refs),
                        "retrieval_strategy": result.retrieval_strategy,
                    },
                )
            )
        return ContextContributorResponse(
            response_format="ksadk.context-response/v1", fragments=tuple(fragments)
        )

    def fit(self, collection, definition, response, remaining):
        """Admit whole records using their actual model envelope, never raw text only."""
        admitted = ContextContributorResponse(
            response_format="ksadk.context-response/v1", fragments=()
        )
        maximum = min(remaining, definition.contributor_policy.capabilities.max_tokens)
        for fragment in response.fragments:
            candidate = admitted.model_copy(update={"fragments": (*admitted.fragments, fragment)})
            counted = sum(
                self._planner._count_message(message)
                for message in contributor_messages(collection, definition, candidate)
            )
            if (
                counted <= maximum
                and len(candidate.model_dump_json(by_alias=True)) <= definition.max_chars
            ):
                admitted = candidate
        return admitted


def bind_native_memory_context(context, scope, planner):
    policy = MemorySpec.model_validate(context.bundle.resolved_agent_spec.get("memory") or {})
    context_spec = ContextSpec.model_validate(
        context.bundle.resolved_agent_spec.get("context") or {}
    )
    if (
        not policy.enabled
        or not policy.recall.enabled
        or not policy.recall.max_tokens
        or not policy.scopes
        or context_spec.contributors.memory_recall is False
    ):
        return ()
    binding, ref = selected_memory_binding(context)
    callback = getattr(binding.runtime, "memory_recall", None)
    if not inspect.iscoroutinefunction(callback):
        # The existing official platform resource owner exposes memory via MCP
        # tools. Keep that path; it is not an automatic recall implementation.
        from ksadk.plugins.providers.platform_resources import PLATFORM_RESOURCE_MCP_REF

        if policy.provider_ref.startswith("binding://") and ref == PLATFORM_RESOURCE_MCP_REF:
            return ()
        raise ValueError("native_memory_binding_invalid")
    if planner is None:
        raise ValueError("native_context_counter_unavailable")
    source = StudioMemoryContextSource(context, scope, planner, binding, policy)
    return ((source.definition, source.read, source.fit),)


def selected_memory_binding(context):
    """One immutable primary capability identity shared by recall and tools."""
    bindings = [item for item in context.memory_sources if item.slot == "memory.primary"]
    if len(bindings) != 1:
        raise ValueError("native_memory_binding_unavailable")
    binding = bindings[0]
    ref = f"plugin://{binding.plugin_id}@{binding.plugin_version}"
    declared = [item for item in context.bundle.composition.profile.capabilities if item.ref == ref]
    offers = [
        offer
        for item in context.bundle.composition.plugin_lock.plugins
        if item.id == binding.plugin_id and item.version == binding.plugin_version
        for offer in item.provides
        if offer.definition == "memory.provider/v1"
        and offer.owner == binding.plugin_id
        and offer.slot == binding.slot
    ]
    if len(declared) != 1 or len(offers) != 1:
        raise ValueError("native_memory_binding_unavailable")
    return binding, ref
