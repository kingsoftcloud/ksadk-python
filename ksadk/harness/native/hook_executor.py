"""Scoped host callbacks for Native HookPort; Kernel owns effect admission.

This executor is not a command/HTTP sandbox or a legacy ProviderHook bridge.
The host must supply authorized callbacks, pinned definitions and live policy.
No result grants a task verdict, tool approval, model budget or replay right.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass

from .hook_runtime_contract import (
    HookDefinition,
    HookInvocationRequest,
    HookInvocationResult,
    HookOutput,
    _json_bytes,
    hook_execution_policy_digest,
    validate_hook_result,
)
from .state import RunScope

HookCallback = Callable[[HookInvocationRequest], Awaitable[HookOutput]]
HookAuthorizer = Callable[[HookInvocationRequest], Awaitable[str]]
HookAvailability = Callable[[], bool | Awaitable[bool]]


def _detached(value):
    return type(value).model_validate_json(_json_bytes(value.model_dump(mode="json")))


@dataclass(frozen=True)
class HookBinding:
    definition: HookDefinition
    callback: HookCallback
    available: HookAvailability | None = None


class ScopedHookExecutor:
    """A HookPort with explicit policy, deadline, byte limits and Task ownership.

    Callback errors are failed only under the host's read-only declaration.
    An admitted mutating callback's missing/invalid outcome remains uncertain.
    Uncooperative tasks remain owned until terminal; their late values are
    discarded. The declared cleanup wait bounds caller/close latency only.
    """

    def __init__(
        self, *, scope: RunScope, bindings: Sequence[HookBinding],
        authorize: HookAuthorizer | None, cleanup_timeout_seconds: float,
    ):
        policy_digest = hook_execution_policy_digest(
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        )
        self._scope = _detached(scope)
        self._authorize = authorize
        self._cleanup_timeout = cleanup_timeout_seconds
        self._bindings: dict[str, HookBinding] = {}
        for item in bindings:
            definition = _detached(item.definition)
            if definition.execution_policy_digest != policy_digest:
                raise ValueError("Hook execution policy mismatch")
            if (not callable(item.callback) or definition.hook_id in self._bindings
                    or (item.available is not None and not callable(item.available))):
                raise ValueError("invalid or duplicate Hook binding")
            self._bindings[definition.hook_id] = HookBinding(
                definition, item.callback, item.available,
            )
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def _forget(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        with suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _available(self, binding: HookBinding) -> bool:
        if self._closed:
            return False
        if binding.available is None:
            return True
        try:
            value = binding.available()
            if inspect.isawaitable(value):
                value = await value
            return value is True and not self._closed
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _stop(self, task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
            await asyncio.wait((task,), timeout=self._cleanup_timeout)

    async def invoke(self, request: HookInvocationRequest) -> HookInvocationResult:
        sealed = _detached(request)
        if sealed.scope != self._scope:
            raise ValueError("Hook scope mismatch")
        binding = self._bindings.get(sealed.definition.hook_id)
        if binding is None or binding.definition != sealed.definition:
            raise ValueError("Hook definition mismatch")

        def rejected(reason, *, uncertain=False):
            return HookInvocationResult.for_request(sealed, reason=reason, uncertain=uncertain)

        if sealed.input_bytes > sealed.definition.max_input_bytes:
            return rejected("input_budget_exceeded")
        executed = False
        revoked = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + sealed.definition.timeout_seconds

        def expired():
            # Task.cancel() is cooperative. A host policy may swallow it;
            # invocation authority must not revive with a late "allow".
            return revoked or loop.time() >= deadline

        async def work():
            nonlocal executed
            if not await self._available(binding):
                return rejected("binding_unavailable")
            if expired():
                return rejected("producer_timeout")
            if self._authorize is None:
                return rejected("policy_denied")
            try:
                decision = await self._authorize(_detached(sealed))
            except asyncio.CancelledError:
                raise
            except Exception:
                return rejected("policy_error")
            if expired():
                return rejected("producer_timeout")
            if type(decision) is not str or decision not in {"allow", "deny"}:
                return rejected("policy_error")
            if decision == "deny":
                return rejected("policy_denied")
            if not await self._available(binding):
                return rejected("binding_unavailable")
            if expired():
                return rejected("producer_timeout")
            executed = True
            try:
                output = await binding.callback(_detached(sealed))
            except asyncio.CancelledError:
                raise
            except Exception:
                return rejected("producer_failed", uncertain=not sealed.definition.read_only)
            if not await self._available(binding):
                return rejected("binding_unavailable", uncertain=not sealed.definition.read_only)
            if expired():
                return rejected("producer_timeout", uncertain=not sealed.definition.read_only)
            try:
                if not isinstance(output, HookOutput):
                    raise ValueError("invalid Hook output type")
                output = _detached(output)
                output_bytes = len(_json_bytes(output.model_dump(mode="json")))
                if output_bytes > binding.definition.max_output_bytes:
                    return rejected(
                        "output_budget_exceeded", uncertain=not sealed.definition.read_only,
                    )
                result = HookInvocationResult.for_request(sealed, output=output)
                result = validate_hook_result(sealed, result)
                if expired():
                    return rejected("producer_timeout", uncertain=not sealed.definition.read_only)
                return result
            except (ValueError, TypeError):
                return rejected("producer_invalid", uncertain=not sealed.definition.read_only)

        task = asyncio.create_task(work())
        self._tasks.add(task)
        task.add_done_callback(self._forget)
        try:
            done, _ = await asyncio.wait((task,), timeout=sealed.definition.timeout_seconds)
            if not done:
                revoked = True
                await self._stop(task)
                return rejected(
                    "producer_timeout", uncertain=executed and not sealed.definition.read_only,
                )
            if task.cancelled():
                return rejected(
                    "binding_unavailable" if self._closed else "producer_failed",
                    uncertain=executed,
                )
            return task.result()
        except asyncio.CancelledError:
            revoked = True
            await self._stop(task)
            raise

    async def aclose(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self._cleanup_timeout)
