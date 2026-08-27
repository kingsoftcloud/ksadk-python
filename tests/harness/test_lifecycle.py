"""Phase 4 生命周期闭环测试（plan §17 验收：Build→Deploy→Activate→Invoke）。"""

from __future__ import annotations

import pytest

from ksadk.harness.lifecycle import (
    BuildPipeline,
    LifecycleError,
    LifecycleStatus,
    LocalLifecycleManager,
)


def _revision_payload() -> dict:
    return {
        "role": {
            "name": "finance-analyst",
            "objective": "财务分析",
            "instructionsRef": "skill://finance-instructions@1",
        },
        "model": {"profileRef": "model-profile://kimi-k3@1.0.0"},
    }


class TestBuild:
    def test_build_produces_manifest_with_hashes(self):
        manifest = BuildPipeline().build(
            revision_payload=_revision_payload(),
            revision_ref="agent-revision://proj-1@3",
        )
        payload = manifest.to_payload()
        assert payload["engine"] == "managed-langgraph"
        assert payload["contentHash"].startswith("sha256:")
        assert payload["artifactDigest"].startswith("sha256:")
        assert payload["modelProfileRef"] == "model-profile://kimi-k3@1.0.0"
        assert manifest.build_id

    def test_build_fails_on_invalid_refs(self):
        with pytest.raises(LifecycleError, match="build failed"):
            LocalLifecycleManager().build(
                revision_payload={"modelProfileRef": "not-a-ref"},
                revision_ref="agent-revision://proj-1@3",
            )


class TestLifecycleClosedLoop:
    def test_full_closed_loop(self):
        """Revision → Build → Deploy → Activate → Invoke 每步有真实产物/状态。"""
        manager = LocalLifecycleManager()
        manifest = manager.build(
            revision_payload=_revision_payload(),
            revision_ref="agent-revision://proj-1@3",
        )
        deployment = manager.deploy(
            manifest=manifest, revision_payload=_revision_payload(), route="local/finance"
        )
        assert deployment.status == LifecycleStatus.DEPLOYED
        assert deployment.health_checked, "Deploy 必须真实 Health Check"
        assert manager.registry.route_of(deployment.deployment_id) == "local/finance"

        active = manager.activate("local/finance")
        assert active.status == LifecycleStatus.ACTIVE

        invoked = manager.invoke(route="local/finance", invocation_id="run-1")
        assert invoked.invocations == ["run-1"], "Invoke 必须经 Active Route 落账"

    def test_invoke_rejected_before_activation(self):
        manager = LocalLifecycleManager()
        manifest = manager.build(
            revision_payload=_revision_payload(),
            revision_ref="agent-revision://proj-1@3",
        )
        manager.deploy(
            manifest=manifest, revision_payload=_revision_payload(), route="local/finance"
        )
        with pytest.raises(LifecycleError, match="no active deployment"):
            manager.invoke(route="local/finance", invocation_id="run-1")

    def test_invoke_rejected_on_unknown_route(self):
        with pytest.raises(LifecycleError):
            LocalLifecycleManager().invoke(route="local/none", invocation_id="run-1")

    def test_activate_requires_deployed(self):
        with pytest.raises(LifecycleError, match="no deployed revision"):
            LocalLifecycleManager().activate("local/finance")

    def test_new_activation_supersedes_previous(self):
        manager = LocalLifecycleManager()
        first = manager.build(
            revision_payload=_revision_payload(),
            revision_ref="agent-revision://proj-1@3",
        )
        manager.deploy(manifest=first, revision_payload=_revision_payload(), route="local/finance")
        first_deployment = manager.activate("local/finance")

        second = manager.build(
            revision_payload=_revision_payload(),
            revision_ref="agent-revision://proj-1@4",
        )
        manager.deploy(manifest=second, revision_payload=_revision_payload(), route="local/finance")
        second_deployment = manager.activate("local/finance")
        assert first_deployment.status == LifecycleStatus.SUPERSEDED
        assert second_deployment.status == LifecycleStatus.ACTIVE

    def test_rollback(self):
        manager = LocalLifecycleManager()
        manifest = manager.build(
            revision_payload=_revision_payload(),
            revision_ref="agent-revision://proj-1@3",
        )
        manager.deploy(
            manifest=manifest, revision_payload=_revision_payload(), route="local/finance"
        )
        manager.activate("local/finance")
        rolled = manager.rollback("local/finance")
        assert rolled.status == LifecycleStatus.ROLLED_BACK
        with pytest.raises(LifecycleError):
            manager.invoke(route="local/finance", invocation_id="run-1")
