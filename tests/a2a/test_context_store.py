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


@pytest.mark.asyncio
@pytest.mark.parametrize("left,right", [
    (("tenant", "alice"), ("tenant", " alice ")),
    (("tenant\x1fuser\x1fx", "y"), ("tenant", "x\x1fuser\x1fy")),
])
async def test_product_context_opaque_identifiers_do_not_alias(tmp_path, left, right):
    owners = [A2AContextIdentity("account", tenant, "user", subject)
              for tenant, subject in (left, right)]
    store = SQLiteA2AContextStore(tmp_path / "owners.sqlite")
    original = await store.resolve_or_create(owners[0], "same-context")
    assert await store.get(owners[1], "same-context") is None
    assert await store.resolve_or_create(owners[1], "same-context") != original


@pytest.mark.asyncio
async def test_subaccount_context_ownership_survives_restart(tmp_path):
    owners = [A2AContextIdentity("account", "tenant", "user", "same-user", sub)
              for sub in ("sub-a", "sub-b")]
    path = tmp_path / "subaccounts.sqlite"
    store = SQLiteA2AContextStore(path)
    original = await store.resolve_or_create(owners[0], "same-context")
    restarted = SQLiteA2AContextStore(path)
    assert await restarted.get(owners[1], "same-context") is None
    assert await restarted.resolve_or_create(owners[1], "same-context") != original
    assert await restarted.get(owners[0], "same-context") == original
