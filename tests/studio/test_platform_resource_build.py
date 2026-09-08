from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ksadk.plugins.bridges.dsh import DshProfileBuildSnapshot
from ksadk.resource_runtime.build_artifacts import (
    ResourceBuildReference,
    restore_resource_build,
)
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_authority import VerifiedResourceAuthority
from ksadk.studio.resource_connections import ResourceConnectionDeclaration
from ksadk.studio.service import StudioService
from tests.resource_runtime.test_studio_resource_config import binding


class _Authority:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    def admit(self, config, *, expected_connection_revision=None):
        self.calls.append((config.binding.id, expected_connection_revision))
        now = datetime.now(timezone.utc)
        return VerifiedResourceAuthority(
            connection_ref="connection-a",
            connection_revision=expected_connection_revision,
            tenant_ref="tenant-a",
            resource_principal_ref="principal-a",
            resource=config.binding.resource,
            allowed_operations=("search_knowledge_base",),
            issuer_endpoint="https://iam.example.test",
            issuer_region="region-a",
            data_endpoint="https://knowledge.example.test",
            observed_at=now,
            expires_at=now + timedelta(minutes=1),
            request_id="fixture-request",
        )


def _profile(*, installation: str = "c") -> DshProfileBuildSnapshot:
    return DshProfileBuildSnapshot.model_validate(
        {
            "projection": {
                "profile": "web",
                "bundles": [
                    "@deepseek-ai/dsh-base",
                    "@kingsoftcloud/dsh-platform-resources",
                    "@kingsoftcloud/dsh-knowledge",
                ],
                "configDigest": "sha256:" + "a" * 64,
                "configBytes": 128,
                "hostVersion": "0.1.1-rc.2",
            },
            "dependencyLockDigest": "sha256:" + "b" * 64,
            "installationDigest": "sha256:" + installation * 64,
        }
    )


def _studio(tmp_path: Path) -> tuple[StudioService, _Authority]:
    studio = StudioService(tmp_path)
    studio.credentials.put_session("RESOURCE_AK", "fixture-access-key")
    studio.credentials.put_session("RESOURCE_SK", "fixture-secret-key")
    studio.resource_connections.save(
        ResourceConnectionDeclaration.model_validate(
            {
                "label": "Knowledge",
                "target": {
                    "connectionRef": "connection-a",
                    "tenantRef": "tenant-a",
                    "principalRef": "principal-a",
                    "endpoint": "https://knowledge.example.test",
                    "authMode": "signed",
                },
                "credentials": {
                    "accessKeyRef": "env://RESOURCE_AK",
                    "secretKeyRef": "env://RESOURCE_SK",
                },
            }
        ),
        expected_revision=0,
    )
    authority = _Authority()
    studio.resource_authority = authority
    studio.dsh_capabilities.capture_resource_build_snapshot = _profile
    return studio, authority


def _draft(studio: StudioService):
    return studio.create_studio_agent(
        agent_id="resource-agent",
        name="Resource Agent",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/resource-agent/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.test/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            instructions=Instructions(system="Search the bound knowledge base."),
            bindings=AgentBindings(plugins=[binding()]),
            security=SecuritySpec(network=NetworkPolicy(mode="open")),
        ),
    )


def test_formal_build_embeds_verified_resource_snapshot_without_secrets(tmp_path: Path) -> None:
    studio, authority = _studio(tmp_path)
    draft = _draft(studio)

    build = studio._build_agent_bundle(draft)

    assert authority.calls == [("binding-a", 1)]
    assert build.resource_build_digest and build.resource_snapshot_digest
    archive = studio.workspace.resolve(build.artifact_path, must_exist=True)
    bundle_root = archive.parent / "agent-bundle"
    manifest = json.loads((bundle_root / "manifest.json").read_text())
    assert manifest["resourceBuildDigest"] == build.resource_build_digest
    assert manifest["resourceSnapshotDigest"] == build.resource_snapshot_digest
    reference = ResourceBuildReference(
        digest=build.resource_build_digest,
        snapshot_digest=build.resource_snapshot_digest,
    )
    resource_manifest, packages = restore_resource_build(
        bundle_root / "platform-resources",
        reference,
        cache_directory=tmp_path / "resource-cache",
    )
    frozen = resource_manifest.snapshot.bindings[0]
    assert frozen.connection_revision == 1
    assert frozen.connection.tenant_ref == "tenant-a"
    assert frozen.config.binding.resource.id == "resource-a"
    assert packages == {}
    serialized = "\n".join(
        path.read_text(errors="ignore")
        for path in bundle_root.rglob("*")
        if path.is_file()
    )
    assert "fixture-access-key" not in serialized
    assert "fixture-secret-key" not in serialized


def test_resource_snapshot_changes_build_identity(tmp_path: Path) -> None:
    studio, _ = _studio(tmp_path)
    draft = _draft(studio)
    first = studio._build_agent_bundle(draft)
    studio.dsh_capabilities.capture_resource_build_snapshot = lambda: _profile(
        installation="d"
    )

    second = studio._build_agent_bundle(draft)

    assert second.id != first.id
    assert second.resource_snapshot_digest != first.resource_snapshot_digest


def test_direct_builder_cannot_bypass_resource_admission(tmp_path: Path) -> None:
    studio, _ = _studio(tmp_path)
    draft = _draft(studio)

    with pytest.raises(StudioError) as captured:
        studio.builder.build(draft)

    assert captured.value.code == "RESOURCE_BUILD_ADMISSION_REQUIRED"
    assert not list((tmp_path / "dist" / "resource-agent").glob("build_*"))


def test_connection_revision_drift_publishes_no_build(tmp_path: Path) -> None:
    studio, _ = _studio(tmp_path)
    draft = _draft(studio)
    original = studio.resource_connections.get("connection-a")

    class _DriftingAuthority(_Authority):
        def admit(self, config, *, expected_connection_revision=None):
            grant = super().admit(
                config,
                expected_connection_revision=expected_connection_revision,
            )
            studio.resource_connections.save(
                ResourceConnectionDeclaration.model_validate(
                    {
                        **original.model_dump(exclude={"revision"}),
                        "credentials": {
                            "accessKeyRef": "env://RESOURCE_AK_NEXT",
                            "secretKeyRef": "env://RESOURCE_SK_NEXT",
                        },
                    }
                ),
                expected_revision=1,
            )
            return grant

    studio.credentials.put_session("RESOURCE_AK_NEXT", "fixture-next-access")
    studio.credentials.put_session("RESOURCE_SK_NEXT", "fixture-next-secret")
    studio.resource_authority = _DriftingAuthority()

    with pytest.raises(StudioError) as captured:
        studio._build_agent_bundle(draft)

    assert captured.value.code == "RESOURCE_CONNECTION_CHANGED"
    assert not list((tmp_path / "dist" / "resource-agent").glob("build_*"))
