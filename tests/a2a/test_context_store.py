from __future__ import annotations

import pytest

from ksadk.a2a.context_store import A2AContextIdentity, SQLiteA2AContextStore


@pytest.mark.asyncio
async def test_sqlite_context_store_is_owner_scoped_and_recovers_after_restart(tmp_path) -> None:
    path = tmp_path / "a2a-context.sqlite3"
    owner_a = A2AContextIdentity(
        account_id="account-a",
        tenant_id="tenant-a",
        caller_principal_type="user",
        caller_principal_id="caller-a",
    )
    owner_b = A2AContextIdentity(
        account_id="account-a",
        tenant_id="tenant-a",
        caller_principal_type="user",
        caller_principal_id="caller-b",
    )

    store = SQLiteA2AContextStore(path)
    first = await store.resolve_or_create(owner_a, "caller-context")
    assert first != "caller-context"
    assert await store.resolve_or_create(owner_a, "caller-context") == first
    assert await store.resolve_or_create(owner_b, "caller-context") != first

    restarted_store = SQLiteA2AContextStore(path)
    assert await restarted_store.resolve_or_create(owner_a, "caller-context") == first
