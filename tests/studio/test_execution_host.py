from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
import pytest_asyncio

from ksadk.harness import HarnessConfig, HarnessReasoningTurn, HarnessRuntimeAdapter
from ksadk.plugins.execution_host import PluginExecutionScope
from ksadk.runtime import RuntimeLaunchContext, StartRequest
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.studio.execution_host import ExecutionHostError, StudioExecutionHost
from ksadk.studio.kernel_registry import StudioBuildKernelRegistry
from ksadk.studio.run_service import StudioRunSpec


class Reasoner:
    def __init__(self):
        self.calls = []

    async def complete(self, *, model, prompt, messages, tools):
        self.calls.append(messages)
        return HarnessReasoningTurn(final_text="真实Kernel执行结果", tool_calls=())


@pytest_asyncio.fixture
async def runtime(tmp_path):
    sessions, reasoner = InMemorySessionService(), Reasoner()
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path),
        build_id="build-one",
        agent_id="agent-one",
        model="fixture-model",
    )
    registry = StudioBuildKernelRegistry(
        resolve_build=lambda _: spec,
        resolve_adapter_provider=lambda _: (
            lambda: HarnessRuntimeAdapter(
                HarnessConfig(model="fixture-model", prompt="test"), reasoner=reasoner
            )
        ),
        session_service=sessions,
        state_dir=tmp_path / "state",
        poll_interval=0.01,
    )
    await registry.start()
    host = StudioExecutionHost(registry, state_path=tmp_path / "host.sqlite")
    scope = PluginExecutionScope(
        "fixture-plugin",
        "sha256:fixture",
        "local-authority",
        "local-studio",
        "owner",
        "local-build:build-one",
        "member-session",
    )

    def authorize(scope, operation):
        if scope.owner_subject != "owner" or scope.authority_ref != "local-authority":
            raise ExecutionHostError("forbidden", "scope forbidden")

    async def policy(scope, context, *, request):
        return {"scope": scope, "context": context}

    unregister = host.register_plugin(
        scope.plugin_id, scope.plugin_digest, authorize=authorize, resolve_policy=policy
    )
    try:
        yield host, scope, registry, sessions, reasoner, unregister
    finally:
        await registry.close()
        await host.close()


async def submit(host, scope, key="delivery-one"):
    await host.ensure_session(scope)
    return await host.submit(
        scope,
        content="处理目标",
        idempotency_key=key,
        grant_id="grant-one",
        causation="decision-one",
        policy_context={"fixture": True},
    )


async def terminal(host, scope, key="delivery-one"):
    for _ in range(200):
        receipt = await host.lookup(scope, key)
        if receipt.run_status in {"succeeded", "failed", "cancelled", "interrupted"}:
            return receipt
        await asyncio.sleep(0.01)
    pytest.fail("Kernel did not settle")


@pytest.mark.asyncio
async def test_host_submits_through_real_kernel_and_observes_without_starting(runtime):
    host, scope, registry, sessions, reasoner, _ = runtime
    first = await submit(host, scope)
    result = await terminal(host, scope)
    assert result.run_status == "succeeded"
    assert result.output == "真实Kernel执行结果"
    assert (await submit(host, scope)).message_id == first.message_id
    assert len(reasoner.calls) == 1
    batch = await host.conversation_events(scope, run_id=result.run_id)
    texts = [item["item"]["payload"].get("text") for item in batch["items"]]
    assert "真实Kernel执行结果" in texts
    assert (await host.conversation_events(scope, run_id=result.run_id, after=batch["cursor"]))[
        "items"
    ] == []
    assert len(reasoner.calls) == 1
    session = await sessions.get_session(scope.session_id)
    assert session.user_id == "owner"
    assert registry.active_runtime_count == 1
    with pytest.raises(ExecutionHostError, match="插件管理"):
        host.require_unreserved_session(scope.session_id)


@pytest.mark.asyncio
async def test_accepted_queue_is_revoked_before_execution_and_barrier_retry_is_stable(runtime):
    host, scope, _, _, reasoner, _ = runtime
    await host.ensure_session(scope)
    paused = await host.set_grant(scope, "grant-one", "suspended", "pause")
    await submit(host, scope)
    await asyncio.sleep(0.03)
    assert (await host.lookup(scope, "delivery-one")).run_id is None
    revoked = await host.set_grant(scope, "grant-one", "revoked", "stop")
    assert revoked.discarded
    assert (await host.lookup(scope, "delivery-one")).status == "rejected"
    assert await host.set_grant(scope, "grant-one", "suspended", "pause") == paused
    assert await host.set_grant(scope, "grant-one", "revoked", "stop") == revoked
    assert not reasoner.calls


@pytest.mark.asyncio
async def test_scope_and_plugin_rechecked_even_for_idempotent_retries(runtime):
    host, scope, _, _, _, unregister = runtime
    await submit(host, scope)
    with pytest.raises(ExecutionHostError, match="scope forbidden"):
        await host.lookup(replace(scope, owner_subject="intruder"), "delivery-one")
    with pytest.raises(ExecutionHostError, match="绑定"):
        await host.lookup(replace(scope, binding_ref="local-build:other"), "delivery-one")
    unregister()
    with pytest.raises(ExecutionHostError, match="授权不可用"):
        await submit(host, scope)


@pytest.mark.asyncio
async def test_opaque_policy_ref_is_bound_to_original_session_and_run(runtime):
    host, scope, registry, _, _, _ = runtime
    await submit(host, scope)
    result = await terminal(host, scope)
    kernel = registry.runtime_for_build("build-one")
    message = await kernel.kernel_store.load_by_idempotency(scope.session_id, "delivery-one")
    ref = message.command.payload["execution_policy_ref"]
    request = StartRequest(
        input="test",
        session_id=scope.session_id,
        user_id="owner",
        metadata={"run_id": result.run_id},
    )
    assert (await host.resolve(ref, request=request))["context"] == {"fixture": True}
    with pytest.raises(ExecutionHostError, match="其他会话或执行"):
        await host.resolve(ref, request=request.model_copy(update={"session_id": "other"}))
    with pytest.raises(ExecutionHostError, match="其他会话或执行"):
        await host.resolve(
            ref, request=request.model_copy(update={"metadata": {"run_id": "other"}})
        )
