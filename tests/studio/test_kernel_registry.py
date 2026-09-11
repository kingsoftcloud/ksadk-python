from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ksadk.events.canonical import RunCompleted
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.harness import HarnessConfig, HarnessReasoningTurn, HarnessRuntimeAdapter
from ksadk.kernel.ingress import map_run_request, trusted_context
from ksadk.runtime import RuntimeLaunchContext
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.studio.kernel_registry import StudioBuildKernelError, StudioBuildKernelRegistry
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.scheduler_runtime import StudioScheduledKernelRegistry


class _Reasoner:
    async def complete(self, *, model, prompt, messages, tools):
        return HarnessReasoningTurn(final_text="complete", tool_calls=())


def _registry(tmp_path: Path, sessions, registry_type=StudioBuildKernelRegistry):
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path),
        build_id="build-a",
        agent_id="agent-a",
        model="fixture-model",
        request_config={"prompt": "fixture"},
        manifest_sha256="sha256:fixture",
    )
    return registry_type(
        resolve_build=lambda _build: spec,
        resolve_adapter_provider=lambda _spec: (
            lambda: HarnessRuntimeAdapter(
                HarnessConfig(model="fixture-model", prompt="fixture"),
                reasoner=_Reasoner(),
                workspace_root=tmp_path,
            )
        ),
        session_service=sessions,
        poll_interval=0.005,
        lease_ttl_seconds=5,
    )


@pytest.mark.asyncio
async def test_generic_and_scheduler_ingress_share_build_runtime(tmp_path: Path) -> None:
    sessions = InMemorySessionService()
    registry = _registry(tmp_path, sessions, StudioScheduledKernelRegistry)
    await registry.start()
    try:
        targets = await asyncio.gather(*(registry.ensure_build("build-a") for _ in range(4)))
        assert all(target is targets[0] for target in targets)
        assert registry.active_runtime_count == 1
        runtime = registry.runtime_for_build("build-a")
        assert registry.spec_for_build("build-a").agent_id == "agent-a"
        for kind in ("studio", "scheduler"):
            target = targets[0]
            context = trusted_context(
                source_kind=kind,
                source_ref=f"{kind}-fixture",
                tenant_id=target.tenant_id,
                agent_instance_id=target.agent_instance_id,
                session_id=kind,
            )
            command = map_run_request(
                session_id=kind,
                idempotency_key=kind,
                content="work",
                trusted=context,
            )
            if kind == "studio":
                await registry.ensure_session(
                    "build-a",
                    kind,
                    "alice",
                    title="Review",
                    metadata={"title_source": "user"},
                )
                receipt = await registry.submit_control(command, context.permit)
            else:
                receipt = await registry.submit(command, context.permit)
            assert receipt.status == "accepted"
            for _ in range(200):
                events = await RuntimeEventStore(sessions).list(kind)
                if any(isinstance(event, RunCompleted) for event in events):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail(f"{kind} did not complete through the shared Kernel")
        assert registry.runtime_for_build("build-a") is runtime
        assert (await sessions.get_session("studio")).title == "Review"
        assert (await sessions.get_session("studio")).user_id == "alice"
        assert (await sessions.get_session("scheduler")).title == "定时任务 · work"
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_session_ownership_and_display_metadata_cannot_grant_access(tmp_path: Path) -> None:
    sessions = InMemorySessionService()
    registry = _registry(tmp_path, sessions)
    await registry.start()
    try:
        await registry.ensure_session("build-a", "owned", "alice", title="Original")
        with pytest.raises(StudioBuildKernelError, match="会话不属于") as owner:
            await registry.ensure_session("build-a", "owned", "mallory", title="Changed")
        assert owner.value.code == "STUDIO_KERNEL_SESSION_OWNER_MISMATCH"
        with pytest.raises(StudioBuildKernelError) as metadata:
            await registry.ensure_session(
                "build-a",
                "new",
                "alice",
                metadata={"owner_subject": "mallory"},
            )
        assert metadata.value.code == "STUDIO_KERNEL_SESSION_METADATA_INVALID"
        assert await sessions.get_session("new") is None
        existing = await registry.ensure_session("build-a", "owned", "alice", title="Changed")
        assert existing.title == "Original"
        await sessions.create_session("different-agent", "alice", "other-agent")
        with pytest.raises(StudioBuildKernelError) as agent:
            await registry.ensure_session("build-a", "other-agent", "alice")
        assert agent.value.code == "STUDIO_KERNEL_SESSION_OWNER_MISMATCH"
        target = await registry.ensure_build("build-a")
        context = trusted_context(
            source_kind="studio",
            source_ref="read-only",
            tenant_id=target.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id="owned",
            operations=("subscribe_events",),
        )
        command = map_run_request(
            session_id="owned",
            idempotency_key="denied",
            content="work",
            trusted=context,
        )
        receipt = await registry.submit_control(command, context.permit)
        assert receipt.status == "rejected"
        assert not await RuntimeEventStore(sessions).list("owned")
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_session_create_race_still_checks_returned_owner(tmp_path: Path) -> None:
    class ConcurrentSessions(InMemorySessionService):
        async def create_session(self, agent_id, user_id, session_id=None):
            return await super().create_session(agent_id, "another-owner", session_id)

    sessions = ConcurrentSessions()
    registry = _registry(tmp_path, sessions)
    await registry.start()
    try:
        with pytest.raises(StudioBuildKernelError) as error:
            await registry.ensure_session("build-a", "race", "alice", title="Changed")
        assert error.value.code == "STUDIO_KERNEL_SESSION_OWNER_MISMATCH"
        assert (await sessions.get_session("race")).title == ""
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_maintenance_fences_all_ingress_then_resumes_same_kernel(tmp_path: Path) -> None:
    sessions = InMemorySessionService()
    registry = _registry(tmp_path, sessions, StudioScheduledKernelRegistry)
    await registry.start()
    try:
        target = await registry.ensure_build("build-a")
        await registry.ensure_session("build-a", "session", "alice")
        context = trusted_context(
            source_kind="system",
            source_ref="maintenance-test",
            tenant_id=target.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id="session",
        )
        command = map_run_request(
            session_id="session",
            idempotency_key="maintenance",
            content="work",
            trusted=context,
        )
        await registry.set_admission_open(False)
        with pytest.raises(StudioBuildKernelError) as error:
            await registry.submit_control(command, context.permit)
        assert error.value.code == "SCHEDULER_MAINTENANCE"
        with pytest.raises(StudioBuildKernelError):
            await registry.ensure_build("a-new-build")
        assert registry.active_runtime_count == 1
        assert not await registry.unsettled_sessions()
        await registry.set_admission_open(True)
        assert await registry.ensure_build("build-a") is target
        assert (await registry.submit_control(command, context.permit)).status == "accepted"
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_local_registry_shares_session_database_for_durable_interactions(tmp_path: Path):
    from ksadk.sessions.local_service import LocalSessionService

    sessions = LocalSessionService(tmp_path / "sessions.sqlite")
    registry = _registry(tmp_path, sessions)
    await registry.start()
    try:
        await registry.ensure_build("build-a")
        runtime = registry.runtime_for_build("build-a")
        assert runtime.kernel_store.db_path == sessions.db_path
        assert runtime.config.driver == "sqlite"
    finally:
        await registry.close()
