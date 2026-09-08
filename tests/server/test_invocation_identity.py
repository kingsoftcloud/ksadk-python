from __future__ import annotations

import pytest
from fastapi import HTTPException

from ksadk.runtime_context import TRUSTED_IDENTITY_METADATA_KEY, PlatformIdentityContext
from ksadk.server.invocation_identity import (
    inject_trusted_invocation_identity,
    resolve_trusted_invocation_identity,
)
from ksadk.server.routes.models import _split_custom_metadata


def test_runtime_identity_headers_are_optional_for_legacy_calls():
    identity = resolve_trusted_invocation_identity(None, None, None, None)
    assert identity.is_empty


def test_runtime_identity_headers_are_all_or_none():
    with pytest.raises(HTTPException) as caught:
        resolve_trusted_invocation_identity("customer-crm", "enterprise-a", None, None)
    assert caught.value.status_code == 401


def test_runtime_identity_headers_build_verified_context():
    identity = resolve_trusted_invocation_identity(
        "customer-crm", "enterprise-a", "user", "user-007"
    )
    assert identity.user_scope == (
        "customer-crm",
        "enterprise-a",
        "user",
        "user-007",
    )


def test_caller_metadata_cannot_create_private_identity_context():
    result = inject_trusted_invocation_identity(
        {TRUSTED_IDENTITY_METADATA_KEY: {"tenant_id": "forged"}, "public": "value"},
        PlatformIdentityContext(),
    )
    assert result == {"public": "value"}


def test_private_identity_key_is_not_echoed_as_public_metadata():
    public, runtime = _split_custom_metadata(
        {TRUSTED_IDENTITY_METADATA_KEY: {"tenant_id": "forged"}, "public": "value"}
    )
    assert public == {"public": "value"}
    assert TRUSTED_IDENTITY_METADATA_KEY not in runtime


def test_verified_header_identity_overwrites_private_metadata_key():
    identity = PlatformIdentityContext(
        identity_namespace="customer-crm",
        tenant_id="enterprise-a",
        subject_type="user",
        subject_id="user-007",
    )
    result = inject_trusted_invocation_identity(
        {TRUSTED_IDENTITY_METADATA_KEY: {"tenant_id": "forged"}}, identity
    )
    assert result[TRUSTED_IDENTITY_METADATA_KEY] == identity.to_payload()
