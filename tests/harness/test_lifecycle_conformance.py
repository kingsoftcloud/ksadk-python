from __future__ import annotations

import asyncio

from ksadk.harness.lifecycle_conformance import (
    LifecycleConformanceCase,
    run_lifecycle_conformance,
)


class _ControlPlane:
    def __init__(self, *, wrong_revision: bool = False, fail_activate: bool = False):
        self.wrong_revision = wrong_revision
        self.fail_activate = fail_activate
        self.revision = ""
        self.deployment = ""

    async def submit_revision(self, revision_ref):  # type: ignore[no-untyped-def]
        self.revision = revision_ref
        return {"revisionRef": "agent-revision://wrong@1" if self.wrong_revision else revision_ref}

    async def build(self, revision_ref):  # type: ignore[no-untyped-def]
        return {"buildRef": "build://one", "revisionRef": revision_ref}

    async def deploy(self, build_ref, route):  # type: ignore[no-untyped-def]
        self.deployment = "deployment://one"
        return {
            "deploymentRef": self.deployment,
            "buildRef": build_ref,
            "status": "awaiting_approval",
        }

    async def approve(self, deployment_ref):  # type: ignore[no-untyped-def]
        return {"deploymentRef": deployment_ref, "status": "approved"}

    async def activate(self, deployment_ref, route):  # type: ignore[no-untyped-def]
        if self.fail_activate:
            raise RuntimeError("authorization=real-secret activation failed")
        return {"deploymentRef": deployment_ref, "route": route, "status": "active"}

    async def invoke(self, route, invocation_id):  # type: ignore[no-untyped-def]
        return {
            "deploymentRef": self.deployment,
            "revisionRef": self.revision,
            "invocationId": invocation_id,
        }

    async def rollback(self, route, revision_ref):  # type: ignore[no-untyped-def]
        self.revision = revision_ref
        return {"route": route, "revisionRef": revision_ref, "status": "active"}


def _case():
    return LifecycleConformanceCase(
        revision_ref="agent-revision://finance@2",
        rollback_revision_ref="agent-revision://finance@1",
        route="finance",
        invocation_id="invoke-1",
    )


def test_full_lifecycle_contract_is_ready():
    report = asyncio.run(
        run_lifecycle_conformance(
            control_plane_id="local", control_plane=_ControlPlane(), case=_case()
        )
    )

    assert report.status == "ready"
    wire = report.to_dict()
    assert wire["schemaVersion"] == 1
    assert wire["summary"] == {"passed": 7, "failed": 0, "skipped": 0}
    assert [item["rule"] for item in wire["findings"]] == [
        "revision.immutable",
        "build.revision_pinned",
        "deploy.build_pinned",
        "deployment.approved",
        "route.activated",
        "route.invoke_effective_revision",
        "route.rollback_effective_revision",
    ]


def test_wrong_effective_revision_blocks_release():
    report = asyncio.run(
        run_lifecycle_conformance(
            control_plane_id="cloud",
            control_plane=_ControlPlane(wrong_revision=True),
            case=_case(),
            required=True,
        )
    )

    assert report.status == "blocked"
    assert report.findings[0].rule == "revision.immutable"
    assert report.findings[0].status == "failed"


def test_execution_failure_is_redacted_and_blocks():
    report = asyncio.run(
        run_lifecycle_conformance(
            control_plane_id="cloud",
            control_plane=_ControlPlane(fail_activate=True),
            case=_case(),
        )
    )

    assert report.status == "blocked"
    assert "real-secret" not in report.findings[-1].detail
    assert "[REDACTED]" in report.findings[-1].detail


def test_missing_optional_and_required_control_planes_are_honest():
    optional = asyncio.run(
        run_lifecycle_conformance(
            control_plane_id="cloud",
            control_plane=None,
            case=_case(),
            unavailable_reason="token=do-not-leak is missing",
        )
    )
    required = asyncio.run(
        run_lifecycle_conformance(
            control_plane_id="cloud", control_plane=None, case=_case(), required=True
        )
    )

    assert optional.status == "not_configured"
    assert optional.findings[0].status == "skipped"
    assert "do-not-leak" not in optional.findings[0].detail
    assert required.status == "blocked"
    assert required.findings[0].status == "failed"
