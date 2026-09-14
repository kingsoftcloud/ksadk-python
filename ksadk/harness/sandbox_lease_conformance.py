"""Storage-agnostic conformance suite for authoritative Sandbox leases."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ksadk.harness.sandbox_lease import (
    SandboxLeaseConflict,
    SandboxLeaseGrant,
    SandboxLeaseProvider,
    SandboxLeaseScope,
)


@dataclass(frozen=True)
class SandboxLeaseConformanceFinding:
    rule: str
    status: str
    detail: str = ""


@dataclass
class SandboxLeaseConformanceReport:
    findings: list[SandboxLeaseConformanceFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(item.status == "failed" for item in self.findings)

    def pass_rule(self, rule: str) -> None:
        self.findings.append(SandboxLeaseConformanceFinding(rule, "passed"))

    def fail_rule(self, rule: str, detail: str) -> None:
        self.findings.append(SandboxLeaseConformanceFinding(rule, "failed", detail))


async def run_sandbox_lease_conformance(
    provider: SandboxLeaseProvider,
    *,
    scope: SandboxLeaseScope,
    backend_id: str,
    handle_id: str,
) -> SandboxLeaseConformanceReport:
    """Verify fencing and namespace semantics with a unique test handle.

    The caller must provide a dedicated test scope and a never-reused
    ``handle_id``.  The suite releases every successfully acquired live grant,
    but authoritative providers may retain their normal audit history.
    """

    report = SandboxLeaseConformanceReport()
    first = await _acquire_first(
        provider,
        scope=scope,
        backend_id=backend_id,
        handle_id=handle_id,
        report=report,
    )
    if first is None:
        return report

    await _verify_exclusive_acquire(provider, first, report)
    await _verify_scope_isolation(provider, first, report)
    renewed = await _verify_renew(provider, first, report)
    if renewed is None:
        await _best_effort_release(provider, first)
        return report
    await _verify_wrong_owner_rejected(provider, renewed, report)
    if not await _verify_release(provider, renewed, report):
        return report
    replacement = await _verify_monotonic_takeover(provider, renewed, report)
    if replacement is None:
        return report
    await _verify_stale_grant_rejected(provider, renewed, report)
    await _best_effort_release(provider, replacement)
    return report


async def _acquire_first(
    provider,
    *,
    scope,
    backend_id,
    handle_id,
    report,
) -> SandboxLeaseGrant | None:
    try:
        grant = await provider.acquire(
            backend_id=backend_id,
            handle_id=handle_id,
            scope=scope,
            owner_id="conformance-owner-a",
            ttl_seconds=30,
        )
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.acquire", _exception_detail(exc))
        return None
    if (
        grant.backend_id == backend_id
        and grant.handle_id == handle_id
        and grant.scope == scope
        and grant.owner_id == "conformance-owner-a"
        and grant.fencing_token > 0
        and grant.expires_at > 0
    ):
        report.pass_rule("lease.acquire")
        return grant
    report.fail_rule("lease.acquire", "provider returned a mismatched grant")
    return grant


async def _verify_exclusive_acquire(provider, grant, report) -> None:
    try:
        await provider.acquire(
            backend_id=grant.backend_id,
            handle_id=grant.handle_id,
            scope=grant.scope,
            owner_id="conformance-owner-b",
            ttl_seconds=30,
        )
    except SandboxLeaseConflict:
        report.pass_rule("lease.exclusive_acquire")
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.exclusive_acquire", _exception_detail(exc))
    else:
        report.fail_rule("lease.exclusive_acquire", "overlapping acquire was accepted")


async def _verify_scope_isolation(provider, grant, report) -> None:
    other_scope = SandboxLeaseScope(
        tenant_id=f"{grant.scope.tenant_id}-other",
        workspace_id=grant.scope.workspace_id,
    )
    other = None
    try:
        other = await provider.acquire(
            backend_id=grant.backend_id,
            handle_id=grant.handle_id,
            scope=other_scope,
            owner_id="conformance-other-scope",
            ttl_seconds=30,
        )
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.scope_isolation", _exception_detail(exc))
        return
    try:
        if other.scope == other_scope:
            report.pass_rule("lease.scope_isolation")
        else:
            report.fail_rule("lease.scope_isolation", "provider changed lease scope")
    finally:
        await _best_effort_release(provider, other)


async def _verify_renew(provider, grant, report) -> SandboxLeaseGrant | None:
    try:
        renewed = await provider.renew(grant, ttl_seconds=60)
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.renew", _exception_detail(exc))
        return None
    if (
        renewed.backend_id == grant.backend_id
        and renewed.handle_id == grant.handle_id
        and renewed.scope == grant.scope
        and renewed.owner_id == grant.owner_id
        and renewed.fencing_token == grant.fencing_token
        and renewed.expires_at > grant.expires_at
    ):
        report.pass_rule("lease.renew")
        return renewed
    report.fail_rule("lease.renew", "renew changed identity/token or did not extend expiry")
    return renewed


async def _verify_wrong_owner_rejected(provider, grant, report) -> None:
    tampered = replace(grant, owner_id="conformance-intruder")
    try:
        await provider.renew(tampered, ttl_seconds=60)
    except SandboxLeaseConflict:
        report.pass_rule("lease.wrong_owner_rejected")
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.wrong_owner_rejected", _exception_detail(exc))
    else:
        report.fail_rule("lease.wrong_owner_rejected", "tampered owner was accepted")


async def _verify_release(provider, grant, report) -> bool:
    try:
        await provider.release(grant)
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.release", _exception_detail(exc))
        return False
    report.pass_rule("lease.release")
    return True


async def _verify_monotonic_takeover(provider, previous, report):
    try:
        replacement = await provider.acquire(
            backend_id=previous.backend_id,
            handle_id=previous.handle_id,
            scope=previous.scope,
            owner_id="conformance-owner-b",
            ttl_seconds=30,
        )
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.monotonic_takeover", _exception_detail(exc))
        return None
    if replacement.fencing_token > previous.fencing_token:
        report.pass_rule("lease.monotonic_takeover")
    else:
        report.fail_rule(
            "lease.monotonic_takeover",
            "replacement fencing token was not monotonic",
        )
    return replacement


async def _verify_stale_grant_rejected(provider, stale, report) -> None:
    renew_rejected = release_rejected = False
    try:
        await provider.renew(stale, ttl_seconds=30)
    except SandboxLeaseConflict:
        renew_rejected = True
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.stale_grant_rejected", _exception_detail(exc))
        return
    try:
        await provider.release(stale)
    except SandboxLeaseConflict:
        release_rejected = True
    except Exception as exc:  # noqa: BLE001
        report.fail_rule("lease.stale_grant_rejected", _exception_detail(exc))
        return
    if renew_rejected and release_rejected:
        report.pass_rule("lease.stale_grant_rejected")
    else:
        report.fail_rule(
            "lease.stale_grant_rejected",
            "stale renew or release was accepted",
        )


async def _best_effort_release(provider, grant) -> None:
    try:
        await provider.release(grant)
    except Exception:  # noqa: BLE001 - cleanup must not mask the finding
        pass


def _exception_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "SandboxLeaseConformanceFinding",
    "SandboxLeaseConformanceReport",
    "run_sandbox_lease_conformance",
]
