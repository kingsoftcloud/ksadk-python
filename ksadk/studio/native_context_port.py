"""Locked DSH Context sources consumed by Native as scoped, bounded data."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ksadk.harness.native.context_contributions import (
    ContextCollectionRequest,
    ContextCollectionResult,
    ContextSourceDefinition,
    ContextSourceResult,
    quoted_context_messages,
)
from ksadk.harness.native.context_contributor_contract import (
    ContextContributorPolicy,
    contributor_exchange,
    contributor_messages,
    contributor_request,
    utc_now,
)
from ksadk.harness.native.context_runtime import ContextPlanner
from ksadk.harness.native.state import RunPhase, RunScope
from ksadk.harness.native.studio_admission import studio_mapping_digest
from ksadk.harness.stores.native_contract import RunStore
from ksadk.plugins.builtins import ReadOnlyBundleContextRuntime
from ksadk.plugins.context_contributor import ContextContributorResponse
from ksadk.plugins.contracts import plugin_lock_digest
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.harness import HarnessEngineActivationContext, HarnessTurnRequest
from ksadk.plugins.providers.harness_context import HarnessContextContributorConfiguration
from ksadk.plugins.resolver import composition_profile_digest
from ksadk.studio.native_memory_context import bind_native_memory_context


@dataclass(frozen=True)
class _BoundSource:
    definition: ContextSourceDefinition
    read: Callable[..., Awaitable]
    fit_response: Callable | None = None


class StudioNativeContextPort:
    def __init__(self, context: HarnessEngineActivationContext, scope: RunScope, store: RunStore,
                 *, planner: ContextPlanner | None = None, workspace_root: Path | None = None):
        if (
            scope.agent_id != context.agent_name
            or scope.agent_id != context.bundle.manifest.agent_id
        ):
            raise ValueError("native_context_agent_unbound")
        self._context, self._scope = context, scope.model_copy(deep=True)
        self._store = store
        self._manifest = context.bundle.manifest.model_dump_json()
        self._profile = context.bundle.composition.profile.model_dump_json()
        self._lock = context.bundle.composition.plugin_lock.model_dump_json()
        self._pending: set[asyncio.Task] = set()
        self._closed = False
        self._planner = planner
        if context.context_sources or context.memory_sources:
            # The source name alone cannot borrow authority from a different
            # activation graph. Recompute the admitted graph, including nested
            # containers that frozen contracts still allow callers to mutate.
            composition = context.bundle.composition
            if (
                context.profile_digest != composition.profile_digest
                or context.profile_digest != context.bundle.manifest.composition_profile_digest
                or context.profile_digest != composition_profile_digest(composition.profile)
                or context.plugin_lock_digest != composition.plugin_lock_digest
                or context.plugin_lock_digest != context.bundle.manifest.plugin_lock_digest
                or context.plugin_lock_digest != plugin_lock_digest(composition.plugin_lock)
            ):
                raise ValueError("native_context_binding_unavailable")
        bound = []
        seen = set()
        for binding in context.context_sources:
            ref = f"plugin://{binding.plugin_id}@{binding.plugin_version}"
            if ref in seen:
                raise ValueError("native_context_binding_duplicate")
            seen.add(ref)
            declarations = [
                item for item in context.bundle.composition.profile.capabilities if item.ref == ref
            ]
            locked = [item for item in context.bundle.composition.plugin_lock.plugins if
                item.id == binding.plugin_id and item.version == binding.plugin_version
            ]
            if len(declarations) != 1 or len(locked) != 1:
                raise ValueError("native_context_binding_unavailable")
            offers = [item for item in locked[0].provides if
                      item.definition == "context.contributor/v1"
                      and item.owner == binding.plugin_id
                      and (binding.slot is None or item.slot == binding.slot)]
            if len(offers) != 1:
                raise ValueError("native_context_binding_unavailable")
            read = getattr(binding.runtime, "harness_context_contribution" if binding.contributor
                           else "harness_context", None)
            if not inspect.iscoroutinefunction(read):
                raise ValueError("native_context_binding_invalid")
            config = declarations[0].config
            compiled = False
            maximum = 32_000
            contributor_policy = None
            rich_config = None
            if binding.contributor:
                if planner is None:
                    raise ValueError("native_context_counter_unavailable")
                rich_config = HarnessContextContributorConfiguration.model_validate_json(
                    HarnessContextContributorConfiguration.model_validate(config).model_dump_json(),
                )
                maximum = rich_config.max_chars
                contributor_policy = ContextContributorPolicy(
                    capabilities=rich_config.capabilities, policy_ref=rich_config.policy_ref,
                    allowed_classifications=rich_config.allowed_classifications,
                    workspace_root=str(workspace_root or context.user_workspace_root
                                       or context.workspace_root),
                    tokenizer_id=planner.tokenizer_id,
                )
            if type(binding.runtime) is ReadOnlyBundleContextRuntime:
                paths = config.get("paths", [])
                if not isinstance(paths, (list, tuple)) or any(
                    not isinstance(path, str) or not path.strip() for path in paths
                ):
                    raise ValueError("native_context_binding_invalid")
                if not paths:
                    continue  # Empty builtin binding is not an I/O obligation.
                maximum = config.get("maxChars", 32_000)
                if type(maximum) is not int or maximum < 1:
                    raise ValueError("native_context_budget_invalid")
                compiled = (
                    tuple(paths) == ("instructions/soul.md",)
                    and context.bundle.resolved_agent_spec.get("soul") is not None
                )
            definition = ContextSourceDefinition(
                source_id="dsh-context-" + hashlib.sha256(ref.encode()).hexdigest()[:24],
                source_ref=ref,
                binding_digest=studio_mapping_digest({
                    "bundle_digest": context.bundle.bundle_digest,
                    "profile_digest": context.profile_digest,
                    "plugin_lock_digest": context.plugin_lock_digest,
                    "source_ref": ref,
                    "slot": offers[0].slot,
                    "policy": ("studio.native-context-contributor/v1" if binding.contributor
                               else "studio.native-context-string-data/v2"),
                    **({"contributor": contributor_policy.model_dump(mode="json")}
                       if contributor_policy is not None else {}),
                }),
                projection=("fragment_sections" if binding.contributor else
                            "compiled_reference" if compiled else "quoted_data"),
                contributor_policy=contributor_policy,
                max_chars=min(maximum, 131_072),
                **({"timeout_seconds": rich_config.capabilities.timeout_ms / 1000,
                    "failure_mode": rich_config.capabilities.failure_mode}
                   if rich_config is not None else {}),
            )
            # Generation is a re-admitted resource identity, never part of the
            # immutable Run configuration. The captured activation probe and
            # Kernel ActivationResourceRef continue to govern its availability.
            # Capture the selected callback now; later monkeypatching a runtime
            # attribute cannot replace the implementation within this binding.
            bound.append(_BoundSource(definition, read))
        bound.extend(_BoundSource(*item) for item in
                     bind_native_memory_context(context, scope, planner))
        self._bound = tuple(bound)

    @property
    def definitions(self):
        return tuple(item.definition.model_copy(deep=True) for item in self._bound)

    def _available(self):
        probe = self._context.activation_available
        return (
            not self._closed and probe is not None and probe() is True
            and self._context.bundle.manifest.model_dump_json() == self._manifest
            and self._context.bundle.composition.profile.model_dump_json() == self._profile
            and self._context.bundle.composition.plugin_lock.model_dump_json() == self._lock
        )

    def _drain(self, task):
        self._pending.discard(task)
        with suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _read(self, bound: _BoundSource, turn: HarnessTurnRequest):
        task = asyncio.create_task(bound.read(self._context.bundle, turn))
        self._pending.add(task)
        task.add_done_callback(self._drain)
        try:
            done, _ = await asyncio.wait((task,), timeout=bound.definition.timeout_seconds)
            if not done:
                raise TimeoutError
            return task.result()
        finally:
            if not task.done():
                # A source that suppresses cancellation cannot extend the
                # declared deadline or send its late value to the Run. Retain
                # ownership and drain it when it eventually settles.
                task.cancel()

    async def aclose(self):
        self._closed = True
        tasks = tuple(self._pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=0.25)

    def _admit_quoted(self, request, result, remaining):
        if remaining is None:
            return result, remaining
        frames = quoted_context_messages(request, (result,))
        counted = sum(self._planner._count_message(frame) for frame in frames)
        if counted > remaining:
            # Even a warn diagnostic consumes input. Never silently exceed
            # the host quota or discard a declared warning to make it fit.
            return (ContextSourceResult.rejected(
                next(s for s in request.sources if s.source_id == result.source_id),
                "collection_budget_exceeded",
            ), remaining)
        return result.model_copy(update={"counted_tokens": counted}), remaining - counted

    async def collect(self, request: ContextCollectionRequest) -> ContextCollectionResult:
        request = ContextCollectionRequest.model_validate_json(request.model_dump_json())
        if request.scope != self._scope or request.sources != self.definitions:
            raise ValueError("native_context_request_unbound")
        snapshot = self._store.load(self._scope, request.run_id)
        intent = self._store.get_intent(self._scope, request.run_id, request.operation_key)
        if (
            snapshot.session_id != request.session_id or snapshot.turn_id != request.turn_id
            or snapshot.goal != request.goal or snapshot.goal_revision != request.goal_revision
            or snapshot.phase != RunPhase.PREPARING_CONTEXT
            or snapshot.pending_intent != request.operation_key
            or intent is None or intent.kind != "context" or intent.status != "running"
            or request.input_budget != intent.payload.get("input_budget")
            or request.model_call_cursor != (
                snapshot.model_calls if request.input_budget is not None else None
            )
        ):
            raise ValueError("native_context_request_unbound")
        results = []
        remaining = request.input_budget
        for bound in self._bound:
            definition = bound.definition
            if not self._available():
                result, remaining = self._admit_quoted(
                    request, ContextSourceResult.rejected(definition, "binding_unavailable"),
                    remaining,
                )
                results.append(result)
                continue
            turn = HarnessTurnRequest(
                user_id=request.scope.user_id, session_id=request.session_id,
                messages=({"role": "user", "content": request.goal},),
                model=dict(self._context.activation_claims).get("model"),
                invocation_id=request.operation_key,
                request_metadata={
                    "authenticated_scope": request.scope.model_dump(mode="json"),
                    "run_id": request.run_id, "turn_id": request.turn_id,
                    "goal_revision": request.goal_revision,
                },
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + definition.timeout_seconds
            try:
                producer_request = (contributor_request(request, definition, remaining)
                                    if definition.contributor_policy is not None else turn)
                text = await self._read(bound, producer_request)
                if not self._available():
                    reason = "binding_unavailable"
                elif loop.time() >= deadline:
                    reason = "source_timeout"
                elif definition.contributor_policy is not None:
                    response = ContextContributorResponse.model_validate(text)
                    if bound.fit_response is not None:
                        response = bound.fit_response(request, definition, response, remaining)
                    exchange = contributor_exchange(request, definition, response, remaining)
                    if any(fragment.expires_at is not None and fragment.expires_at <= utc_now()
                           for fragment in exchange.response.fragments):
                        reason = "source_expired"
                    elif (len(exchange.response.model_dump_json(by_alias=True))
                          > definition.max_chars):
                        reason = "source_budget_exceeded"
                    else:
                        exchange.project(now=utc_now())
                        counted = sum(self._planner._count_message(message) for message in
                                      contributor_messages(request, definition, exchange.response))
                        if counted > min(remaining,
                                         definition.contributor_policy.capabilities.max_tokens):
                            reason = "source_budget_exceeded"
                        elif loop.time() >= deadline:
                            reason = "source_timeout"
                        else:
                            results.append(ContextSourceResult.contributed(
                                definition, exchange.response, counted,
                            ))
                            remaining -= counted
                            continue
                elif not isinstance(text, str):
                    reason = "producer_invalid"
                elif len(text) > definition.max_chars:
                    reason = "source_budget_exceeded"
                else:
                    accepted = ContextSourceResult.accepted(definition, text)
                    accepted, remaining = self._admit_quoted(request, accepted, remaining)
                    results.append(accepted)
                    continue
            except TimeoutError:
                reason = "source_timeout"
            except PluginHostError as error:
                reason = (
                    "source_integrity"
                    if error.code == "builtin_context_source_integrity"
                    else "producer_failed"
                )
            except (ValueError, TypeError):
                reason = "producer_invalid"
            except Exception:
                reason = "producer_failed"  # Never persist exception bodies.
            rejected, remaining = self._admit_quoted(
                request, ContextSourceResult.rejected(definition, reason), remaining,
            )
            results.append(rejected)
        return ContextCollectionResult.for_request(request, results)
