"""Project authenticated, frozen Context Contributors into the service protocol."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ksadk.harness.service_runtime import ServiceContextSource
from ksadk.plugins.context_contributor import (
    AuthenticatedContextScope,
    ContextContributorExchange,
    ContextContributorRequest,
    ContextContributorResponse,
)
from ksadk.plugins.contracts import plugin_lock_digest
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.harness_context import (
    HarnessContextContributorConfiguration,
    HarnessContextContributorSource,
)
from ksadk.plugins.resolver import composition_profile_digest
from ksadk.runtime import RunHandle

if TYPE_CHECKING:
    from ksadk.plugins.providers.harness import HarnessEngineActivationContext


def _locked_graph(context: HarnessEngineActivationContext) -> None:
    graph = context.bundle.composition
    if (
        context.profile_digest != graph.profile_digest
        or context.profile_digest != composition_profile_digest(graph.profile)
        or context.profile_digest != context.bundle.manifest.composition_profile_digest
        or context.plugin_lock_digest != graph.plugin_lock_digest
        or context.plugin_lock_digest != plugin_lock_digest(graph.plugin_lock)
        or context.plugin_lock_digest != context.bundle.manifest.plugin_lock_digest
    ):
        raise PluginHostError(
            "harness_service_context_unbound", "Context differs from the locked plugin graph",
        )


@dataclass
class _Contributor:
    context: HarnessEngineActivationContext
    runtime: HarnessContextContributorSource
    config: HarnessContextContributorConfiguration
    scope: dict[str, str]
    cached: tuple[tuple[str, int, int], float, list[dict[str, Any]]] | None = None

    def check_available(self) -> None:
        available = self.context.activation_available
        if available is not None and not available():
            raise ValueError("Harness context activation is unavailable")
        _locked_graph(self.context)

    async def read(self, handle: RunHandle, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.check_available()
        remaining = params["remainingTokens"]
        revision = params["goalRevision"]
        if type(remaining) is not int or remaining < 0 or type(revision) is not int or revision < 0:
            raise ValueError("Invalid Harness context budget or goal revision")
        if handle.session_id != self.scope["session_id"]:
            raise ValueError("Harness context session differs from activation")
        budget = min(remaining, self.config.capabilities.max_tokens)
        cache_key = (handle.run_id, revision, budget)
        cache = self.cached
        if cache is not None and cache[0] == cache_key and cache[1] > time.time():
            return cache[2]
        workspace = self.context.user_workspace_root or self.context.workspace_root
        request = ContextContributorRequest(
            request_format="ksadk.context-request/v1",
            scope=AuthenticatedContextScope(
                authenticated=True,
                actor_id=self.scope["user_id"],
                agent_id=self.scope["agent_id"],
                session_id=handle.session_id,
                turn_id=params["turnId"],
            ),
            invocation_id=params["operationKey"],
            user_input=params["goal"],
            workspace_root=str(workspace),
            policy_ref=self.config.policy_ref,
            remaining_budget=budget,
            allowed_classifications=self.config.allowed_classifications,
            metadata={
                "tenant_id": self.scope["tenant_id"], "workspace_id": self.scope["workspace_id"],
                "run_id": handle.run_id, "goal_revision": revision,
            },
        )
        response = await self.runtime.harness_context_contribution(self.context.bundle, request)
        if not isinstance(response, ContextContributorResponse):
            raise ValueError("Invalid Harness context response")
        exchange = ContextContributorExchange(
            contract_format="ksadk.context-contributor/v1", capabilities=self.config.capabilities,
            request=request, response=response,
        )
        exchange.project()
        self.check_available()
        if len(response.fragments) > 64 or len(response.model_dump_json()) > self.config.max_chars:
            raise ValueError("Harness context response exceeded its grant")
        items = []
        expires = float("inf")
        for fragment in response.fragments:
            role = fragment.metadata.get("role")
            privileged = fragment.kind == "compiled_prompt" or (
                isinstance(role, str) and role in {"system", "developer"}
            )
            items.append({
                "projection": "instructions" if privileged else "data",
                "fragment": fragment.model_dump(mode="json", by_alias=True),
                **({"expiresAt": int(fragment.expires_at.timestamp() * 1000)}
                   if fragment.expires_at else {}),
            })
            if fragment.expires_at is not None:
                expires = min(expires, fragment.expires_at.timestamp())
        if self.config.capabilities.cacheability != "none":
            self.cached = (cache_key, expires, items)
        return items


def service_context_sources(
    context: HarnessEngineActivationContext,
) -> tuple[ServiceContextSource, ...]:
    bindings = tuple(binding for binding in context.context_sources if binding.contributor)
    if not bindings:
        return ()
    claims = dict(context.activation_claims)
    if (
        any(not claims.get(key, "").strip() for key in ("tenant_id", "workspace_id", "user_id"))
        or claims.get("agent_id") != context.agent_name
        or claims.get("session_id") != context.activation_key
        or not context.activation_key
    ):
        raise PluginHostError(
            "harness_service_context_unbound", "Context requires authenticated activation claims",
        )
    _locked_graph(context)
    graph = context.bundle.composition
    result = []
    for binding in bindings:
        ref = f"plugin://{binding.plugin_id}@{binding.plugin_version}"
        declarations = [item for item in graph.profile.capabilities if item.ref == ref]
        offers = [
            offer for entry in graph.plugin_lock.plugins for offer in entry.provides
            if entry.id == binding.plugin_id and entry.version == binding.plugin_version
            and offer.owner == binding.plugin_id and offer.definition == "context.contributor/v1"
            and offer.slot == binding.slot
        ]
        if len(declarations) != 1 or len(offers) != 1 or not isinstance(
            binding.runtime, HarnessContextContributorSource
        ):
            raise PluginHostError(
                "harness_service_context_unbound", "Context source differs from the locked offer",
            )
        config = HarnessContextContributorConfiguration.model_validate(declarations[0].config)
        caps = config.capabilities
        if not caps.max_tokens:
            continue
        identity = json.dumps([
            context.bundle.bundle_digest, context.profile_digest, context.plugin_lock_digest,
            ref, binding.slot, config.model_dump(mode="json"), claims,
        ], ensure_ascii=False, sort_keys=True)
        source = _Contributor(context, binding.runtime, config, claims)
        result.append(ServiceContextSource({
            "sourceId": "ksadk-context-" + hashlib.sha256(ref.encode()).hexdigest()[:24],
            "bindingDigest": "sha256:" + hashlib.sha256(identity.encode()).hexdigest(),
            "maxTokens": caps.max_tokens, "maxBytes": config.max_chars,
            "timeoutMs": caps.timeout_ms, "failureMode": caps.failure_mode,
            "allowInstructions": caps.trust_level in {"developer", "platform"},
        }, source.read))
    return tuple(result)
