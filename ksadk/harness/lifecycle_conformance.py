"""Lifecycle control-plane conformance contract.

The SDK owns the contract and verification logic, while the local lifecycle manager or a
remote control plane owns the implementation.  An absent remote backend is reported as
``not_configured`` rather than being replaced by a fake successful deployment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

_SECRET_DETAIL = re.compile(
    r"(?i)(authorization|api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"
)


def _safe_detail(value: object) -> str:
    return _SECRET_DETAIL.sub(r"\1=[REDACTED]", str(value or ""))[:512]


class LifecycleControlPlane(Protocol):
    """Minimum control-plane behavior required by a managed Harness deployment."""

    async def submit_revision(self, revision_ref: str) -> Mapping[str, Any]: ...

    async def build(self, revision_ref: str) -> Mapping[str, Any]: ...

    async def deploy(self, build_ref: str, route: str) -> Mapping[str, Any]: ...

    async def approve(self, deployment_ref: str) -> Mapping[str, Any]: ...

    async def activate(self, deployment_ref: str, route: str) -> Mapping[str, Any]: ...

    async def invoke(self, route: str, invocation_id: str) -> Mapping[str, Any]: ...

    async def rollback(self, route: str, revision_ref: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class LifecycleConformanceCase:
    revision_ref: str
    route: str
    invocation_id: str
    rollback_revision_ref: str | None = None


@dataclass(frozen=True)
class LifecycleConformanceFinding:
    rule: str
    status: str
    detail: str


@dataclass(frozen=True)
class LifecycleConformanceReport:
    schema_version: int
    control_plane_id: str
    status: str
    required: bool
    findings: tuple[LifecycleConformanceFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        counts = {
            state: sum(1 for item in self.findings if item.status == state)
            for state in ("passed", "failed", "skipped")
        }
        return {
            "schemaVersion": self.schema_version,
            "controlPlaneId": self.control_plane_id,
            "status": self.status,
            "required": self.required,
            "findings": [
                {"rule": item.rule, "status": item.status, "detail": item.detail}
                for item in self.findings
            ],
            "summary": counts,
        }


def _value(payload: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return str(value)
    return ""


def _finding(rule: str, passed: bool, detail: str) -> LifecycleConformanceFinding:
    return LifecycleConformanceFinding(
        rule=rule,
        status="passed" if passed else "failed",
        detail=_safe_detail(detail),
    )


async def run_lifecycle_conformance(
    *,
    control_plane_id: str,
    control_plane: LifecycleControlPlane | None,
    case: LifecycleConformanceCase,
    required: bool = False,
    unavailable_reason: str = "not configured",
) -> LifecycleConformanceReport:
    """Verify Revision -> Build -> Approval -> Deploy -> Activate -> Invoke -> Rollback."""

    if control_plane is None:
        status = "blocked" if required else "not_configured"
        finding = LifecycleConformanceFinding(
            rule="lifecycle.configuration",
            status="failed" if required else "skipped",
            detail=_safe_detail(unavailable_reason),
        )
        return LifecycleConformanceReport(
            schema_version=1,
            control_plane_id=control_plane_id,
            status=status,
            required=required,
            findings=(finding,),
        )

    findings: list[LifecycleConformanceFinding] = []
    try:
        submitted = await control_plane.submit_revision(case.revision_ref)
        effective_revision = _value(submitted, "revisionRef", "revision_ref")
        findings.append(
            _finding(
                "revision.immutable",
                effective_revision == case.revision_ref,
                f"requested={case.revision_ref} effective={effective_revision or '<missing>'}",
            )
        )

        built = await control_plane.build(case.revision_ref)
        build_ref = _value(built, "buildRef", "build_ref")
        build_revision = _value(built, "revisionRef", "revision_ref")
        findings.append(
            _finding(
                "build.revision_pinned",
                bool(build_ref) and build_revision == case.revision_ref,
                f"build={build_ref or '<missing>'} revision={build_revision or '<missing>'}",
            )
        )
        if not build_ref:
            raise RuntimeError("build response omitted buildRef")

        deployed = await control_plane.deploy(build_ref, case.route)
        deployment_ref = _value(deployed, "deploymentRef", "deployment_ref")
        deploy_build = _value(deployed, "buildRef", "build_ref")
        findings.append(
            _finding(
                "deploy.build_pinned",
                bool(deployment_ref) and deploy_build == build_ref,
                f"deployment={deployment_ref or '<missing>'} build={deploy_build or '<missing>'}",
            )
        )
        if not deployment_ref:
            raise RuntimeError("deploy response omitted deploymentRef")

        approved = await control_plane.approve(deployment_ref)
        approval_status = _value(approved, "status").lower()
        findings.append(
            _finding(
                "deployment.approved",
                approval_status in {"approved", "passed"},
                f"status={approval_status or '<missing>'}",
            )
        )

        activated = await control_plane.activate(deployment_ref, case.route)
        active_deployment = _value(activated, "deploymentRef", "deployment_ref")
        findings.append(
            _finding(
                "route.activated",
                active_deployment == deployment_ref,
                f"route={case.route} deployment={active_deployment or '<missing>'}",
            )
        )

        invoked = await control_plane.invoke(case.route, case.invocation_id)
        invoked_deployment = _value(invoked, "deploymentRef", "deployment_ref")
        invoked_revision = _value(invoked, "revisionRef", "revision_ref")
        findings.append(
            _finding(
                "route.invoke_effective_revision",
                invoked_deployment == deployment_ref
                and invoked_revision == case.revision_ref,
                (
                    f"deployment={invoked_deployment or '<missing>'} "
                    f"revision={invoked_revision or '<missing>'}"
                ),
            )
        )

        if case.rollback_revision_ref:
            rolled_back = await control_plane.rollback(
                case.route, case.rollback_revision_ref
            )
            rollback_revision = _value(rolled_back, "revisionRef", "revision_ref")
            findings.append(
                _finding(
                    "route.rollback_effective_revision",
                    rollback_revision == case.rollback_revision_ref,
                    f"revision={rollback_revision or '<missing>'}",
                )
            )
    except Exception as exc:  # noqa: BLE001 - conformance must preserve evidence
        findings.append(
            LifecycleConformanceFinding(
                rule="lifecycle.execution",
                status="failed",
                detail=_safe_detail(exc),
            )
        )

    status = "blocked" if any(item.status == "failed" for item in findings) else "ready"
    return LifecycleConformanceReport(
        schema_version=1,
        control_plane_id=control_plane_id,
        status=status,
        required=required,
        findings=tuple(findings),
    )


__all__ = [
    "LifecycleConformanceCase",
    "LifecycleConformanceFinding",
    "LifecycleConformanceReport",
    "LifecycleControlPlane",
    "run_lifecycle_conformance",
]
