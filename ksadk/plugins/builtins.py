"""Built-in capability runtimes for immutable AgentBundle execution.

These plugins deliberately consume only the already validated
``ResolvedPluginBundle`` plus explicit process services supplied at host
construction.  They never reach back into Studio's mutable catalogs.  The
module also keeps rendering as a projection of the canonical ConversationItem
contract; it does not introduce another event or transcript pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from pathlib import PurePosixPath as PurePosixPath
from typing import Any, cast

from ksadk.conversations.contracts import ConversationItem
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.harness.config import McpToolSpec
from ksadk.plugins.builtin_resources import (
    _bound_capability_config as _bound_capability_config,
)
from ksadk.plugins.builtin_resources import (
    _directory_digest as _directory_digest,
)
from ksadk.plugins.builtin_resources import (
    _directory_resource_digests as _directory_resource_digests,
)
from ksadk.plugins.builtin_resources import (
    _is_secret_reference as _is_secret_reference,
)
from ksadk.plugins.builtin_resources import (
    _mapping as _mapping,
)
from ksadk.plugins.builtin_resources import (
    _match_resource as _match_resource,
)
from ksadk.plugins.builtin_resources import (
    _optional_string as _optional_string,
)
from ksadk.plugins.builtin_resources import (
    _required_string as _required_string,
)
from ksadk.plugins.builtin_resources import (
    _resolved_resources as _resolved_resources,
)
from ksadk.plugins.builtin_resources import (
    _resource_entries as _resource_entries,
)
from ksadk.plugins.builtin_resources import (
    _safe_bundle_path as _safe_bundle_path,
)
from ksadk.plugins.builtin_resources import (
    _string_list as _string_list,
)
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import (
    CompositionProfile,
    PluginManifest,
)
from ksadk.plugins.contracts import (
    plugin_lock_digest as plugin_lock_digest,
)
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.memory_provider import (
    MemoryFlushRequest,
    MemoryMutationRequest,
    MemoryRecallRequest,
    MemoryReceiptRequest,
    MemoryRecordRequest,
    memory_db_path,
    memory_scope_bindings,
)
from ksadk.plugins.providers.harness import HarnessSkillContribution, HarnessTurnRequest
from ksadk.plugins.resolver import composition_profile_digest as composition_profile_digest
from ksadk.sessions.base import BaseSessionService
from ksadk.sessions.local_service import LocalSessionService

BUILTIN_PLUGIN_VERSION = "1.0.0"

SQLITE_SESSION_STORE_PLUGIN_ID = "io.ksadk.session-store.sqlite"
WORKSPACE_MCP_PLUGIN_ID = "io.ksadk.mcp.workspace"
WORKSPACE_SKILL_PLUGIN_ID = "io.ksadk.skill.workspace"
READ_ONLY_CONTEXT_PLUGIN_ID = "io.ksadk.context.bundle-readonly"
LOCAL_MEMORY_PROVIDER_PLUGIN_ID = "io.ksadk.memory.local-default"
CORE_RENDERER_PLUGIN_ID = "io.ksadk.renderer.conversation-core"

SecretResolver = Callable[[str], str | None]


def _manifest_digest(plugin_id: str) -> str:
    payload = f"{plugin_id}@{BUILTIN_PLUGIN_VERSION}:builtin-runtime-v1".encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _manifest(
    plugin_id: str,
    *,
    definition: str,
    slot: str,
    mode: str,
    permissions: Sequence[str] = (),
    secret_fields: Sequence[str] = (),
) -> PluginManifest:
    return cast(
        PluginManifest,
        PluginManifest.model_validate(
            {
                "metadata": {"id": plugin_id, "version": BUILTIN_PLUGIN_VERSION},
                "spec": {
                    "domain": "ksadk-platform",
                    "runtime": "python",
                    "entrypoint": "ksadk.plugins.builtins:builtin_capability_factories",
                    "provides": [{"definition": definition, "slot": slot, "mode": mode}],
                    "secretFields": list(secret_fields),
                    "permissions": list(permissions),
                    "isolation": "in-process",
                    "compatibility": {
                        "kernelApi": ">=1,<2",
                        "runtimeProtocols": ["agentkit.runtime/v1"],
                        "python": ">=3.12,<3.15",
                    },
                    "healthContract": "plugin.health/v1",
                    "provenance": {
                        "source": "builtin",
                        "digest": _manifest_digest(plugin_id),
                        "license": "Apache-2.0",
                    },
                },
            },
        ),
    )


def builtin_capability_manifests() -> tuple[PluginManifest, ...]:
    """Return the exact, deterministic Release built-in capability catalog."""

    return (
        _manifest(
            SQLITE_SESSION_STORE_PLUGIN_ID,
            definition="session.event-store/v1",
            slot="session.events",
            mode="unique",
            permissions=("filesystem:session-store",),
        ),
        _manifest(
            WORKSPACE_MCP_PLUGIN_ID,
            definition="mcp.connector/v1",
            slot="mcp.workspace",
            mode="multiple",
            permissions=("network:mcp",),
            secret_fields=("apiKeyRef",),
        ),
        _manifest(
            WORKSPACE_SKILL_PLUGIN_ID,
            definition="skill.source/v1",
            slot="skill.workspace",
            mode="multiple",
            permissions=("filesystem:bundle-read",),
        ),
        _manifest(
            READ_ONLY_CONTEXT_PLUGIN_ID,
            definition="context.contributor/v1",
            slot="context.bundle",
            mode="multiple",
            permissions=("filesystem:bundle-read",),
        ),
        _manifest(
            LOCAL_MEMORY_PROVIDER_PLUGIN_ID,
            definition="memory.provider/v1",
            slot="memory.primary",
            mode="unique",
            permissions=("filesystem:memory-store",),
        ),
        _manifest(
            CORE_RENDERER_PLUGIN_ID,
            definition="session.item.renderer/v1",
            slot="renderer.core",
            mode="multiple",
        ),
    )


def builtin_capability_permissions(
    profile: CompositionProfile,
) -> frozenset[str]:
    """Return host-owned permissions for built-ins selected by one Profile.

    Studio's built-in session store, context, renderer, MCP and Skill adapters
    are part of the trusted PluginHost distribution. Their narrow permissions
    are granted by that host rather than copied into every user-authored Agent
    revision. Only exact built-in capability references present in the resolved
    Profile contribute permissions; AgentProvider permissions remain subject to
    the Agent's explicit security policy.
    """

    selected_refs = {capability.ref for capability in profile.capabilities}
    permissions: set[str] = set()
    for manifest in builtin_capability_manifests():
        reference = f"plugin://{manifest.metadata.id}@{manifest.metadata.version}"
        if reference in selected_refs:
            permissions.update(manifest.spec.permissions)
    return frozenset(permissions)


class _BuiltinRuntime:
    def __init__(self, plugin_id: str, version: str) -> None:
        self.plugin_id = plugin_id
        self.version = version
        self._ready = False
        self._disposed = False

    async def start(self) -> None:
        if self._disposed:
            raise PluginHostError(
                "builtin_capability_disposed",
                f"built-in capability {self.plugin_id}@{self.version} is disposed",
            )
        self._ready = True

    async def health(self) -> bool:
        return self._ready and not self._disposed

    async def drain(self) -> None:
        self._ready = False

    async def dispose(self) -> None:
        self._ready = False
        self._disposed = True

    def _require_ready(self) -> None:
        if not self._ready or self._disposed:
            raise PluginHostError(
                "builtin_capability_unavailable",
                f"built-in capability {self.plugin_id}@{self.version} is not ready",
            )

    def _config(self, bundle: ResolvedPluginBundle) -> Mapping[str, Any]:
        self._require_ready()
        return _bound_capability_config(bundle, self.plugin_id, self.version)


class SQLiteSessionStoreRuntime(_BuiltinRuntime):
    """Own one durable SQLite session service for an active profile."""

    def __init__(self, plugin_id: str, version: str, *, db_path: Path) -> None:
        super().__init__(plugin_id, version)
        self.db_path = db_path.resolve()
        self._service: LocalSessionService | None = None
        self._event_store: SessionServiceEventStore | None = None

    async def start(self) -> None:
        if self._disposed:
            await super().start()
        try:
            self._service = LocalSessionService(self.db_path)
            self._event_store = SessionServiceEventStore(self._service)
        except Exception as error:  # noqa: BLE001 - durable backend boundary
            self._service = None
            self._event_store = None
            raise PluginHostError(
                "builtin_session_store_start_failed",
                "SQLite session store could not be initialized",
            ) from error
        self._ready = True

    def session_service_for(self, bundle: ResolvedPluginBundle) -> BaseSessionService:
        self._config(bundle)
        if self._service is None:  # pragma: no cover - protected by lifecycle
            raise PluginHostError(
                "builtin_capability_unavailable", "SQLite session service is unavailable"
            )
        return self._service

    def event_store_for(self, bundle: ResolvedPluginBundle) -> SessionServiceEventStore:
        self._config(bundle)
        if self._event_store is None:  # pragma: no cover - protected by lifecycle
            raise PluginHostError(
                "builtin_capability_unavailable", "SQLite event store is unavailable"
            )
        return self._event_store

    async def dispose(self) -> None:
        service = self._service
        self._ready = False
        self._disposed = True
        if service is not None:
            await service.aclose()


class LocalMemoryProviderRuntime(_BuiltinRuntime):
    """Own lazy tenant databases; the immutable Build grants each recall scope."""

    def __init__(self, plugin_id: str, version: str, *, state_root: Path):
        super().__init__(plugin_id, version)
        self._state_root = state_root.resolve()
        self._providers: dict[str, Any] = {}
        self._provider_lock = threading.Lock()
        self._pending: set[asyncio.Task] = set()

    async def memory_recall(self, bundle: ResolvedPluginBundle, request: MemoryRecallRequest):
        from ksadk.memory.models import MemorySearchResult
        from ksadk.studio.contracts import MemorySpec

        self._config(bundle)
        request = MemoryRecallRequest.model_validate_json(request.model_dump_json())
        policy = MemorySpec.model_validate(bundle.resolved_agent_spec.get("memory") or {})
        if (not policy.enabled or not policy.recall.enabled or not request.max_tokens
                or not request.query.strip() or not request.scopes):
            return MemorySearchResult("not_configured", [], None, "local-default", 0, "exact")
        if (request.principal.agent_id != bundle.manifest.agent_id
                or policy.provider_ref != "local-default"
                or set(request.scopes) - set(policy.scopes)
                or request.top_k > policy.recall.top_k
                or request.max_tokens > policy.recall.max_tokens
                or request.min_score != policy.recall.min_score):
            raise PluginHostError("memory_recall_unbound", "Memory recall exceeds its Build grant")
        return await self._owned(self._recall, request)

    async def memory_search(self, bundle: ResolvedPluginBundle, request: MemoryRecallRequest):
        """A model-directed read retains Build bounds without automatic injection."""
        request = MemoryRecallRequest.model_validate_json(request.model_dump_json())
        policy = self._memory_policy(bundle, request.principal)
        if (set(request.scopes) - set(policy.scopes)
                or request.top_k > policy.recall.top_k
                or request.max_tokens > policy.recall.max_tokens
                or request.min_score != policy.recall.min_score):
            raise PluginHostError("memory_search_unbound", "Memory search exceeds its Build grant")
        if not request.query.strip():
            raise PluginHostError("memory_search_query_missing", "Memory search requires a query")
        if not request.max_tokens or not request.scopes:
            from ksadk.memory.models import MemorySearchResult

            return MemorySearchResult("not_configured", [], None, "local-default", 0, "exact")
        return await self._owned(self._recall, request)

    async def _owned(self, callback, request):
        task = asyncio.create_task(asyncio.to_thread(callback, request))
        self._pending.add(task)
        task.add_done_callback(self._drain_read)
        # Cancellation cannot detach a database operation from its owner.
        return await asyncio.shield(task)

    def _drain_read(self, task):
        self._pending.discard(task)
        if not task.cancelled():
            task.exception()

    def _provider(self, principal):
        from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider

        with self._provider_lock:
            self._require_ready()
            tenant = principal.tenant_id
            if tenant not in self._providers:
                self._providers[tenant] = SqliteMemoryProvider(
                    db_path=memory_db_path(self._state_root, principal),
                    tenant_id=tenant, workspace_id=principal.workspace_id,
                )
            return self._providers[tenant]

    def _recall(self, request):
        from dataclasses import replace

        from ksadk.memory.models import MemorySearchRequest

        provider = self._provider(request.principal)
        result = provider.search(MemorySearchRequest(
            query=request.query,
            scopes=list(memory_scope_bindings(request.principal, request.scopes)),
            memory_types=["profile", "fact", "episode"], top_k=request.top_k,
            max_tokens=request.max_tokens, min_score=request.min_score,
            filters={"keyword_relevance": True, "defer_token_budget": True,
                     "tenant_id": request.principal.tenant_id},
        ))
        return replace(result, provider="local-default",
                       retrieval_strategy="keyword")

    def _memory_policy(self, bundle, principal):
        from ksadk.studio.contracts import MemorySpec

        self._config(bundle)
        policy = MemorySpec.model_validate(bundle.resolved_agent_spec.get("memory") or {})
        if (not policy.enabled or policy.provider_ref != "local-default"
                or principal.agent_id != bundle.manifest.agent_id):
            raise PluginHostError("memory_unbound", "Memory is outside its Build grant")
        return policy

    async def memory_read(self, bundle, request: MemoryRecordRequest):
        request = MemoryRecordRequest.model_validate_json(request.model_dump_json())
        policy = self._memory_policy(bundle, request.principal)
        if set(request.scopes) - set(policy.scopes):
            raise PluginHostError("memory_unbound", "Memory is outside its Build scopes")
        return await self._owned(self._read_record, request)

    def _read_record(self, request):
        record = self._provider(request.principal).get(request.memory_id)
        allowed = memory_scope_bindings(request.principal, request.scopes)
        if (record is None or record.tenant_id != request.principal.tenant_id
                or (record.scope != "org" and record.workspace_id != request.principal.workspace_id)
                or (record.scope, record.scope_id) not in allowed
                or record.status != "active" or record.sensitivity == "high"):
            return None
        return record

    async def memory_mutate(self, bundle, request: MemoryMutationRequest):
        from ksadk.memory.models import MemoryCandidate
        from ksadk.memory.policy import MemoryPolicy
        from ksadk.studio.contracts import ContextSpec

        request = MemoryMutationRequest.model_validate_json(request.model_dump_json())
        policy = self._memory_policy(bundle, request.principal)
        rollout = ContextSpec.model_validate(
            bundle.resolved_agent_spec.get("context") or {}
        ).rollout.memory_write
        if (policy.write.mode == "off" or rollout == "off" or request.scope not in policy.scopes
                or (policy.write.mode == "explicit_only" and request.authority == "candidate")
                or (request.action == "forget" and request.authority == "candidate")):
            raise PluginHostError("memory_write_disabled", "Memory write is not granted")
        scope, scope_id = memory_scope_bindings(request.principal, (request.scope,))[0]
        reason = "explicit_user_request" if request.authority != "candidate" else "model_guess"
        candidate = MemoryCandidate(
            candidate_id=request.operation_key, operation={"remember": "add", "correct": "update",
                                                        "forget": "delete"}[request.action],
            memory_type="profile", scope=scope, scope_id=scope_id, content=request.content,
            confidence=1 if request.authority != "candidate" else 0, importance=0.8,
            source_event_ids=[request.source_event_id], reason=reason,
            conflicts_with=[request.memory_id] if request.memory_id else [],
        )
        evaluation = MemoryPolicy().evaluate(candidate)
        if evaluation.decision != "commit":
            return {"committed": False, "decision": evaluation.decision,
                    "code": evaluation.reason}
        if rollout == "shadow":
            return {"committed": False, "decision": "shadow", "action": request.action}
        return await self._owned(self._mutate, request)

    def _mutate(self, request):
        from ksadk.memory.extraction import native_user_memory_candidate
        from ksadk.memory.models import MemoryRecord
        from ksadk.memory.policy import content_hash

        scope, scope_id = memory_scope_bindings(request.principal, (request.scope,))[0]
        record = MemoryRecord(
            memory_id=request.memory_id or "", tenant_id=request.principal.tenant_id,
            workspace_id=request.principal.workspace_id, scope=scope, scope_id=scope_id,
            memory_type="profile", content=request.content, summary=request.content,
            status="active", confidence=1, importance=0.8,
            valid_from="", valid_to="", expires_at="",
            source_session_id=request.source_session_id, source_event_ids=[request.source_event_id],
            source_seq_range=None, content_hash=content_hash(request.content), version=1,
            metadata={"source_goal_revision": request.source_goal_revision,
                      "source_goal_digest": request.source_goal_digest,
                      "mutation_session_id": request.source_session_id},
            write_policy="user_confirm",
        )
        consumed = []
        if request.scope == "user":
            for source in request.source_user_observations:
                candidate = native_user_memory_candidate(
                    source.content, source_event_id=source.source_event_id,
                    scope=scope, scope_id=scope_id,
                )
                if candidate is not None:
                    consumed.append((candidate, source.source_digest))
        return self._provider(request.principal).apply_mutation(
            record, action=request.action, expected_version=request.expected_version,
            operation_key=request.operation_key, request_digest=request.request_digest,
            consumed_user_candidates=tuple(consumed),
        )

    async def memory_receipt(self, bundle, request: MemoryReceiptRequest):
        request = MemoryReceiptRequest.model_validate_json(request.model_dump_json())
        self._memory_policy(bundle, request.principal)
        return await self._owned(self._receipt, request)

    async def memory_flush(self, bundle, request: MemoryFlushRequest):
        """Evaluate actual user observations under the locked Native write policy."""
        from ksadk.memory.extraction import native_user_memory_candidate
        from ksadk.memory.policy import MemoryPolicy
        from ksadk.studio.contracts import ContextSpec

        request = MemoryFlushRequest.model_validate_json(request.model_dump_json())
        policy = self._memory_policy(bundle, request.principal)
        rollout = ContextSpec.model_validate(
            bundle.resolved_agent_spec.get("context") or {},
        ).rollout.memory_write
        if (policy.write.mode == "off" or rollout == "off" or "user" not in policy.scopes
                or request.trigger == "pre_compaction"
                and not policy.write.flush_before_compaction
                or request.trigger == "completed_turn" and not policy.write.flush_after_turn):
            return {"status": "disabled", "proposed": 0, "committed": 0, "results": []}
        scope, scope_id = memory_scope_bindings(request.principal, ("user",))[0]
        candidates = []
        for observation in request.observations:
            candidate = native_user_memory_candidate(
                observation.content, source_event_id=observation.source_event_id,
                scope=scope, scope_id=scope_id,
            )
            if candidate is not None and (
                policy.write.mode != "explicit_only" or candidate.reason.startswith("explicit_user")
            ):
                candidates.append((observation, candidate))
        if not candidates:
            return {"status": "empty", "proposed": 0, "committed": 0, "results": []}
        if rollout == "shadow":
            # Shadow decisions live in the caller's effect receipt, not in a
            # Provider database, pending-observation counter or durable fact.
            return {"status": "shadow", "proposed": len(candidates), "committed": 0,
                    "results": [{"decision": MemoryPolicy().evaluate(item).decision}
                                for _source, item in candidates]}
        return await self._owned(self._flush, (request, policy, candidates))

    def _flush(self, data):
        from ksadk.memory.extraction import NATIVE_USER_EXTRACTION_POLICY

        request, policy, candidates = data
        provider = self._provider(request.principal)
        results = []
        for observation, candidate in candidates:
            identity = {"principal": request.principal.model_dump(mode="json"),
                        "source_event_id": observation.source_event_id,
                        "policy": NATIVE_USER_EXTRACTION_POLICY,
                        "memory": policy.model_dump(mode="json")}
            # The original source identity is stable across compaction/Turn
            # consumers under the same grant. Changed grants need a distinct
            # evaluation receipt; the original source marker still prevents
            # counting that evaluation as another user observation.
            key = "native-memory-candidate:" + hashlib.sha256(
                json.dumps(identity, sort_keys=True, ensure_ascii=False).encode(),
            ).hexdigest()
            payload = {**identity, "source": observation.model_dump(mode="json")}
            request_digest = "sha256:" + hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(),
            ).hexdigest()
            results.append(provider.apply_candidate(
                candidate, operation_key=key, request_digest=request_digest,
                source_session_id=observation.source_session_id,
                source_digest=observation.source_digest,
                tenant_id=request.principal.tenant_id,
                workspace_id=request.principal.workspace_id,
            ))
        return {"status": "evaluated", "proposed": len(candidates),
                "committed": sum(item["committed"] for item in results), "results": results}

    def _receipt(self, request):
        return self._provider(request.principal).mutation_receipt(
            request.operation_key, request.request_digest
        )

    async def dispose(self):
        self._ready = False
        self._disposed = True
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        with self._provider_lock:
            for provider in self._providers.values():
                provider.close()
            self._providers.clear()


class WorkspaceMCPRuntime(_BuiltinRuntime):
    """Materialize locked workspace MCP resources for the Harness provider."""

    def __init__(
        self,
        plugin_id: str,
        version: str,
        *,
        secret_resolver: SecretResolver | None,
    ) -> None:
        super().__init__(plugin_id, version)
        self._secret_resolver = secret_resolver

    def inventory(self, bundle: ResolvedPluginBundle) -> tuple[dict[str, Any], ...]:
        resources = self._resources(bundle)
        inventory: list[dict[str, Any]] = []
        for materializer, resolved in resources:
            inventory.append(
                {
                    "name": resolved["name"],
                    "version": resolved["version"],
                    "transport": resolved["transport"],
                    "endpointUrl": resolved.get("endpointUrl"),
                    "toolFilter": tuple(_string_list(materializer.get("toolFilter"))),
                }
            )
        return tuple(inventory)

    def harness_mcp_specs(self, bundle: ResolvedPluginBundle) -> tuple[McpToolSpec, ...]:
        specs: list[McpToolSpec] = []
        for materializer, resolved in self._resources(bundle):
            transport = _required_string(resolved, "transport", code="builtin_mcp_invalid")
            if transport not in {"http", "sse"}:
                raise PluginHostError(
                    "builtin_mcp_transport_unsupported",
                    f"workspace MCP {resolved['name']!r} uses unsupported transport",
                )
            endpoint = _required_string(
                resolved, "endpointUrl", code="builtin_mcp_endpoint_missing"
            )
            secret_ref = _optional_string(materializer.get("apiKeyRef"))
            if secret_ref is None:
                # 组合器只透传 binding.config；目录资源的凭据声明在合同
                # envRefs 里（例如 Authorization: env://<API_KEY>）。
                # materializer 未显式覆盖时回退读取资源自身 envRefs，
                # 否则 http MCP 少鉴权头必然 401 transport_failed。
                env_refs = resolved.get("envRefs")
                if isinstance(env_refs, Mapping) and env_refs:
                    if len(env_refs) > 1:
                        raise PluginHostError(
                            "builtin_mcp_credentials_unsupported",
                            f"workspace MCP {resolved['name']!r} accepts at most "
                            "one Authorization credential",
                        )
                    header_name, reference = next(iter(env_refs.items()))
                    if str(header_name).strip().casefold() != "authorization":
                        raise PluginHostError(
                            "builtin_mcp_credentials_unsupported",
                            f"workspace MCP {resolved['name']!r} envRefs header "
                            f"{header_name!r} is not supported; use Authorization",
                        )
                    secret_ref = str(reference).strip()
            api_key: str | None = None
            if secret_ref is not None:
                if not _is_secret_reference(secret_ref):
                    raise PluginHostError(
                        "builtin_mcp_secret_ref_invalid",
                        "workspace MCP apiKeyRef must be an external secret reference",
                    )
                if self._secret_resolver is None:
                    raise PluginHostError(
                        "builtin_mcp_secret_unavailable",
                        "workspace MCP credential resolver is unavailable",
                    )
                try:
                    api_key = self._secret_resolver(secret_ref)
                except Exception as error:  # noqa: BLE001 - secret-provider boundary
                    raise PluginHostError(
                        "builtin_mcp_secret_unavailable",
                        "workspace MCP credential could not be resolved",
                    ) from error
                if not api_key:
                    raise PluginHostError(
                        "builtin_mcp_secret_unavailable",
                        "workspace MCP credential could not be resolved",
                    )
            specs.append(
                McpToolSpec(
                    name=_required_string(resolved, "name", code="builtin_mcp_invalid"),
                    url=endpoint,
                    api_key=api_key,
                    tool_filter=tuple(_string_list(materializer.get("toolFilter"))),
                    tool_name_prefix=_optional_string(materializer.get("toolNamePrefix")),
                    # McpToolSpec.description 声明为 str：无静态描述时给 ""，
                    # 不要传 None（下游按 str 消费）。
                    description=_optional_string(materializer.get("description")) or "",
                )
            )
        return tuple(specs)

    def _resources(
        self, bundle: ResolvedPluginBundle
    ) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]], ...]:
        config = self._config(bundle)
        declared = _resource_entries(config, expected_kind="mcp")
        resolved = _resolved_resources(bundle, "mcpServers")
        return tuple(
            (
                _mapping(entry.get("materializer"), code="builtin_mcp_invalid"),
                _match_resource(entry, resolved, code="builtin_mcp_resource_mismatch"),
            )
            for entry in declared
        )


class WorkspaceSkillRuntime(_BuiltinRuntime):
    """Expose integrity-checked Skill instructions copied into the Bundle."""

    def harness_skills(
        self, bundle: ResolvedPluginBundle
    ) -> tuple[HarnessSkillContribution, ...]:
        config = self._config(bundle)
        declared = _resource_entries(config, expected_kind="skill")
        resolved = _resolved_resources(bundle, "skills")
        contributions: list[HarnessSkillContribution] = []
        for entry in declared:
            item = _match_resource(entry, resolved, code="builtin_skill_resource_mismatch")
            name = _required_string(item, "name", code="builtin_skill_invalid")
            bundle_path = _required_string(item, "bundlePath", code="builtin_skill_path_invalid")
            directory = _safe_bundle_path(
                bundle,
                bundle_path,
                code="builtin_skill_path_invalid",
                require_declared=False,
            )
            if not directory.is_dir() or directory.is_symlink():
                raise PluginHostError(
                    "builtin_skill_path_invalid", f"Skill {name!r} is not a Bundle directory"
                )
            expected_digest = _required_string(item, "digest", code="builtin_skill_invalid")
            if _directory_digest(directory) != expected_digest:
                raise PluginHostError(
                    "builtin_skill_digest_mismatch",
                    f"Skill {name!r} does not match its locked Bundle digest",
                )
            source_ref = _required_string(entry, "resourceId", code="builtin_skill_invalid")
            instructions = _required_string(
                item, "instructions", code="builtin_skill_instructions_missing"
            )
            contributions.append(
                HarnessSkillContribution(
                    name=name,
                    instructions=instructions,
                    resource_root=directory,
                    source_ref=source_ref,
                    content_digest=expected_digest,
                )
            )
        if not contributions:
            raise PluginHostError(
                "builtin_skill_resource_missing", "workspace Skill has no locked resource"
            )
        if len({item.source_ref for item in contributions}) != len(contributions):
            raise PluginHostError(
                "builtin_skill_invalid", "workspace Skills must have unique locked resource IDs"
            )
        return tuple(contributions)

    def harness_skill(self, bundle: ResolvedPluginBundle) -> HarnessSkillContribution:
        """Retain the singular ABI for existing providers and direct consumers."""
        contributions = self.harness_skills(bundle)
        if len(contributions) == 1:
            return contributions[0]
        return HarnessSkillContribution(
            name="workspace-skills",
            instructions="\n\n".join(
                f"Skill {item.name}:\n{item.instructions}" for item in contributions
            ),
        )


class ReadOnlyBundleContextRuntime(_BuiltinRuntime):
    """Read declared UTF-8 context files from the immutable Bundle only."""

    async def harness_context(
        self, bundle: ResolvedPluginBundle, request: HarnessTurnRequest
    ) -> str:
        del request
        config = self._config(bundle)
        paths = _string_list(config.get("paths"))
        max_chars = config.get("maxChars", 32_000)
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
            raise PluginHostError(
                "builtin_context_config_invalid", "maxChars must be a positive integer"
            )
        chunks: list[str] = []
        remaining = max_chars
        for relative in paths:
            path = _safe_bundle_path(
                bundle,
                relative,
                code="builtin_context_path_invalid",
                require_declared=True,
            )
            if not path.is_file() or path.is_symlink():
                raise PluginHostError(
                    "builtin_context_path_invalid",
                    f"Bundle context path is not a regular file: {relative}",
                )
            # Resolution verifies the Bundle once; an activation can outlive
            # changes on disk. Authorize and project the same byte snapshot,
            # before truncating it to the configured character budget.
            entry_path = PurePosixPath(relative).as_posix()
            entries = [item for item in bundle.manifest.files if item.path == entry_path]
            try:
                content = path.read_bytes()
            except OSError as error:
                raise PluginHostError(
                    "builtin_context_read_failed",
                    f"Bundle context file could not be read: {relative}",
                ) from error
            if (
                len(entries) != 1
                or len(content) != entries[0].size
                or "sha256:" + hashlib.sha256(content).hexdigest() != entries[0].sha256
            ):
                raise PluginHostError(
                    "builtin_context_source_integrity",
                    f"Bundle context file no longer matches its locked bytes: {relative}",
                )
            try:
                text = content.decode("utf-8").strip()
            except UnicodeError as error:
                raise PluginHostError(
                    "builtin_context_read_failed",
                    f"Bundle context file could not be read: {relative}",
                ) from error
            if not text or remaining == 0:
                continue
            chunk = text[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
        return "\n\n".join(chunks)


class CoreConversationRendererRuntime(_BuiltinRuntime):
    """Project canonical items into a safe core renderer view model."""

    _COMPONENTS = {
        "user_message": "markdown",
        "assistant_text": "markdown",
        "reasoning": "reasoning",
        "tool_call": "tool",
        "approval": "approval",
        "progress": "progress",
        "plan": "plan",
        "goal": "goal",
        "artifact": "artifact",
        "a2ui": "a2ui",
        "error": "error",
    }

    def render(self, item: ConversationItem) -> dict[str, Any]:
        base: dict[str, Any] = {
            "component": self._COMPONENTS.get(item.kind, "unknown"),
            "itemId": item.item_id,
            "lifecycle": item.lifecycle,
        }
        if item.kind in {"user_message", "assistant_text"}:
            text = item.payload.get("text", "")
            base["text"] = text if isinstance(text, str) else str(text)
            return base
        if item.kind == "unknown":
            base["schemaRef"] = item.payload_schema_ref
            base["summary"] = json.dumps(
                item.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )[:4096]
            return base
        # Structured payload stays data.  Consumers choose trusted local UI
        # components; no Bundle-provided HTML or JavaScript is executed here.
        base["payload"] = dict(item.payload)
        base["schemaRef"] = item.payload_schema_ref
        return base


class SQLiteSessionStoreFactory:
    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root.resolve()
        self.runtime: SQLiteSessionStoreRuntime | None = None

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> SQLiteSessionStoreRuntime:
        del services
        _require_manifest(manifest, SQLITE_SESSION_STORE_PLUGIN_ID)
        profile_key = composition_profile_digest(profile).removeprefix("sha256:")
        runtime = SQLiteSessionStoreRuntime(
            manifest.metadata.id,
            manifest.metadata.version,
            db_path=self._state_root / profile_key / "sessions.sqlite",
        )
        self.runtime = runtime
        return runtime


class LocalMemoryProviderFactory:
    def __init__(self, state_root: Path):
        self._state_root = state_root.resolve()

    async def stage(self, manifest, *, profile, services):
        del profile, services
        _require_manifest(manifest, LOCAL_MEMORY_PROVIDER_PLUGIN_ID)
        return LocalMemoryProviderRuntime(
            manifest.metadata.id, manifest.metadata.version, state_root=self._state_root,
        )


class _WorkspaceMCPFactory:
    def __init__(self, secret_resolver: SecretResolver | None) -> None:
        self._secret_resolver = secret_resolver

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> WorkspaceMCPRuntime:
        del profile
        _require_manifest(manifest, WORKSPACE_MCP_PLUGIN_ID)
        resolver = self._secret_resolver
        service_resolver = services.get("secret_resolver")
        if resolver is None and callable(service_resolver):
            resolver = service_resolver
        return WorkspaceMCPRuntime(
            manifest.metadata.id,
            manifest.metadata.version,
            secret_resolver=resolver,
        )


class _SimpleFactory:
    def __init__(self, plugin_id: str, runtime_type: type[_BuiltinRuntime]) -> None:
        self._plugin_id = plugin_id
        self._runtime_type = runtime_type

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> _BuiltinRuntime:
        del profile, services
        _require_manifest(manifest, self._plugin_id)
        return self._runtime_type(manifest.metadata.id, manifest.metadata.version)


def builtin_capability_factories(
    *,
    state_root: Path,
    secret_resolver: SecretResolver | None = None,
) -> dict[str, Any]:
    """Create factories with explicit state and secret-provider boundaries."""

    return {
        SQLITE_SESSION_STORE_PLUGIN_ID: SQLiteSessionStoreFactory(state_root),
        WORKSPACE_MCP_PLUGIN_ID: _WorkspaceMCPFactory(secret_resolver),
        WORKSPACE_SKILL_PLUGIN_ID: _SimpleFactory(WORKSPACE_SKILL_PLUGIN_ID, WorkspaceSkillRuntime),
        READ_ONLY_CONTEXT_PLUGIN_ID: _SimpleFactory(
            READ_ONLY_CONTEXT_PLUGIN_ID, ReadOnlyBundleContextRuntime
        ),
        LOCAL_MEMORY_PROVIDER_PLUGIN_ID: LocalMemoryProviderFactory(state_root),
        CORE_RENDERER_PLUGIN_ID: _SimpleFactory(
            CORE_RENDERER_PLUGIN_ID, CoreConversationRendererRuntime
        ),
    }


def _require_manifest(manifest: PluginManifest, plugin_id: str) -> None:
    if manifest.metadata.id != plugin_id or manifest.metadata.version != BUILTIN_PLUGIN_VERSION:
        raise PluginHostError(
            "builtin_manifest_mismatch",
            f"factory cannot stage {manifest.metadata.id}@{manifest.metadata.version}",
        )


__all__ = [
    "BUILTIN_PLUGIN_VERSION",
    "CORE_RENDERER_PLUGIN_ID",
    "LOCAL_MEMORY_PROVIDER_PLUGIN_ID",
    "READ_ONLY_CONTEXT_PLUGIN_ID",
    "SQLITE_SESSION_STORE_PLUGIN_ID",
    "WORKSPACE_MCP_PLUGIN_ID",
    "WORKSPACE_SKILL_PLUGIN_ID",
    "CoreConversationRendererRuntime",
    "ReadOnlyBundleContextRuntime",
    "SQLiteSessionStoreFactory",
    "SQLiteSessionStoreRuntime",
    "WorkspaceMCPRuntime",
    "WorkspaceSkillRuntime",
    "builtin_capability_factories",
    "builtin_capability_manifests",
    "builtin_capability_permissions",
]
