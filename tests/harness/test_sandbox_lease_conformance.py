from __future__ import annotations

import asyncio
from dataclasses import replace

from ksadk.harness.sandbox_lease import (
    SandboxLeaseConflict,
    SandboxLeaseGrant,
    SandboxLeaseScope,
)
from ksadk.harness.sandbox_lease_conformance import run_sandbox_lease_conformance


def _run(coro):
    return asyncio.run(coro)


class _CompliantLeaseProvider:
    def __init__(self) -> None:
        self.now = 100.0
        self.current = {}
        self.last_fencing = {}

    async def acquire(
        self,
        *,
        backend_id,
        handle_id,
        scope,
        owner_id,
        ttl_seconds,
    ):
        key = (scope, backend_id, handle_id)
        if key in self.current:
            raise SandboxLeaseConflict("owned")
        token = self.last_fencing.get(key, 0) + 1
        grant = SandboxLeaseGrant(
            backend_id=backend_id,
            handle_id=handle_id,
            scope=scope,
            owner_id=owner_id,
            fencing_token=token,
            expires_at=self.now + ttl_seconds,
        )
        self.current[key] = grant
        self.last_fencing[key] = token
        return grant

    async def renew(self, grant, *, ttl_seconds):
        key = (grant.scope, grant.backend_id, grant.handle_id)
        if self.current.get(key) != grant:
            raise SandboxLeaseConflict("stale")
        renewed = replace(grant, expires_at=self.now + ttl_seconds)
        self.current[key] = renewed
        return renewed

    async def release(self, grant):
        key = (grant.scope, grant.backend_id, grant.handle_id)
        if self.current.get(key) != grant:
            raise SandboxLeaseConflict("stale")
        self.current.pop(key)


class _UnscopedLeaseProvider(_CompliantLeaseProvider):
    async def acquire(
        self,
        *,
        backend_id,
        handle_id,
        scope,
        owner_id,
        ttl_seconds,
    ):
        canonical_scope = SandboxLeaseScope("global", "global")
        return await super().acquire(
            backend_id=backend_id,
            handle_id=handle_id,
            scope=canonical_scope,
            owner_id=owner_id,
            ttl_seconds=ttl_seconds,
        )


class _ReusedFencingProvider(_CompliantLeaseProvider):
    async def acquire(self, **kwargs):
        grant = await super().acquire(**kwargs)
        return replace(grant, fencing_token=1)


_SCOPE = SandboxLeaseScope("tenant-conformance", "workspace-conformance")


def test_compliant_lease_provider_passes_all_rules():
    report = _run(
        run_sandbox_lease_conformance(
            _CompliantLeaseProvider(),
            scope=_SCOPE,
            backend_id="sandbox-conformance",
            handle_id="handle-compliant",
        )
    )

    assert report.passed, report.findings
    assert {item.rule for item in report.findings} == {
        "lease.acquire",
        "lease.exclusive_acquire",
        "lease.scope_isolation",
        "lease.renew",
        "lease.wrong_owner_rejected",
        "lease.release",
        "lease.monotonic_takeover",
        "lease.stale_grant_rejected",
    }


def test_conformance_detects_scope_collapse():
    report = _run(
        run_sandbox_lease_conformance(
            _UnscopedLeaseProvider(),
            scope=_SCOPE,
            backend_id="sandbox-conformance",
            handle_id="handle-unscoped",
        )
    )

    failures = {item.rule for item in report.findings if item.status == "failed"}
    assert "lease.acquire" in failures
    assert "lease.scope_isolation" in failures


def test_conformance_detects_reused_fencing_token():
    report = _run(
        run_sandbox_lease_conformance(
            _ReusedFencingProvider(),
            scope=_SCOPE,
            backend_id="sandbox-conformance",
            handle_id="handle-reused-token",
        )
    )

    failures = {item.rule for item in report.findings if item.status == "failed"}
    assert "lease.monotonic_takeover" in failures
