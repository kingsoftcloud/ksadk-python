import asyncio
from uuid import uuid4

import pytest

from ksadk.kernel.teams_execution_context import (
    ContextConflict,
    PostgresTeamsExecutionContextRegistry,
    PreparedTeamsContext,
    SQLiteTeamsExecutionContextRegistry,
    StoreIdentityMismatch,
)
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


async def test_registry_replay_is_shared_and_context_is_frozen(host_harness):
    h = host_harness
    peer = type(h.registry)(h.store)
    assert await peer.initialize() == h.incarnation
    await asyncio.gather(
        *[
            registry.put(h.context, expected_incarnation=h.incarnation)
            for registry in (h.registry, peer)
        ]
    )
    loaded = await peer.get(h.context.context_ref, expected_incarnation=h.incarnation)
    assert loaded == h.context
    changed = h.context.model_dump(mode="json")
    changed["owner_subject"] = "another-owner"
    with pytest.raises(ContextConflict):
        await peer.put(
            PreparedTeamsContext.model_validate(changed), expected_incarnation=h.incarnation
        )
    assert (
        await h.registry.get(h.context.context_ref, expected_incarnation=h.incarnation) == h.context
    )
    loaded.context["role"] = "mutated after read"
    assert (await peer.get(h.context.context_ref, expected_incarnation=h.incarnation)).context == {
        "role": "worker"
    }


async def test_conflicting_command_rolls_back_new_session_reservation(host_harness):
    h = host_harness
    await h.registry.put(h.context, expected_incarnation=h.incarnation)
    values = h.context.model_dump(mode="json")
    values["context_ref"] = "second-context"
    values["command"]["payload"]["teams_context_ref"] = "second-context"
    # Reusing command/key under another context must conflict and leave no row.
    with pytest.raises(ContextConflict):
        await h.registry.put(
            PreparedTeamsContext.model_validate(values), expected_incarnation=h.incarnation
        )
    assert await h.registry.get("second-context", expected_incarnation=h.incarnation) is None


async def test_incarnation_mismatch_never_returns_missing(host_harness):
    h = host_harness
    with pytest.raises(StoreIdentityMismatch):
        await h.registry.get("unknown", expected_incarnation=str(uuid4()))


async def test_store_replacement_and_restart_have_distinct_proofs(host_harness, tmp_path):
    h = host_harness
    await h.registry.put(h.context, expected_incarnation=h.incarnation)
    if h.backend == "sqlite":
        from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore

        await h.store.close()
        reopened = SQLiteAgentKernelStore(h.store.db_path, h.events)
        await reopened.ensure_schema()
        registry = SQLiteTeamsExecutionContextRegistry(reopened)
        assert await registry.initialize() == h.incarnation
        assert (
            await registry.get(h.context.context_ref, expected_incarnation=h.incarnation)
            == h.context
        )
        await reopened.close()
        replacement = SQLiteAgentKernelStore(tmp_path / "fresh.sqlite", h.events)
        await replacement.ensure_schema()
        registry = SQLiteTeamsExecutionContextRegistry(replacement)
        try:
            assert await registry.initialize() != h.incarnation
            with pytest.raises(StoreIdentityMismatch):
                await registry.get(h.context.context_ref, expected_incarnation=h.incarnation)
        finally:
            await replacement.close()
    else:
        import asyncpg

        from ksadk.kernel.postgres_store import PostgresAgentKernelStore

        # Independent connection, same database, same durable identity.
        pool = await asyncpg.create_pool(h.sessions.dsn)
        peer_store = PostgresAgentKernelStore(pool, None, tenant_id=h.store.tenant_id)
        registry = PostgresTeamsExecutionContextRegistry(peer_store)
        try:
            assert await registry.initialize() == h.incarnation
            assert (
                await registry.get(h.context.context_ref, expected_incarnation=h.incarnation)
                == h.context
            )
            # An empty replacement schema is a different inbox, even in one DB.
            schema = "replacement_" + uuid4().hex
            async with pool.acquire() as connection:
                await connection.execute(f'CREATE SCHEMA "{schema}"')
                await connection.execute(f'SET search_path TO "{schema}"')
                fresh = PostgresAgentKernelStore(connection, None, tenant_id=h.store.tenant_id)
                await fresh.ensure_schema()
                replacement = PostgresTeamsExecutionContextRegistry(fresh)
                assert await replacement.initialize() != h.incarnation
                with pytest.raises(StoreIdentityMismatch):
                    await replacement.get(h.context.context_ref, expected_incarnation=h.incarnation)
        finally:
            await pool.close()


async def test_registry_conflict_rolls_back_owner_and_context_together(host_harness):
    h = host_harness
    await h.registry.put(h.context, expected_incarnation=h.incarnation)
    values = h.context.model_dump(mode="json")
    values["context_ref"] = "new-context"
    values["command"]["payload"]["teams_context_ref"] = "new-context"
    values["ref"]["sessionId"] = values["command"]["session_id"] = values["grant"]["session_id"] = (
        "s2"
    )
    values["ref"]["commandId"] = values["command"]["command_id"] = str(uuid4())
    values["ref"]["idempotencyKey"] = values["command"]["idempotency_key"] = "new-key"
    # The original globally unique policy ref conflicts after owner insertion.
    with pytest.raises(ContextConflict):
        await h.registry.put(
            PreparedTeamsContext.model_validate(values), expected_incarnation=h.incarnation
        )
    values["owner_subject"] = "different-owner-after-rollback"
    values["policy_ref"] = values["command"]["payload"]["execution_policy_ref"] = "new-policy"
    fixed = PreparedTeamsContext.model_validate(values)
    await h.registry.put(fixed, expected_incarnation=h.incarnation)
    assert await h.registry.get("new-context", expected_incarnation=h.incarnation) == fixed
