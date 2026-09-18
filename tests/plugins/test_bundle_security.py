"""Security admission tests for immutable Bundle v2 material."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ksadk.plugins.bundle_security import BundleSecurityError, assert_bundle_security
from ksadk.studio.capabilities import compute_bundle_digest
from ksadk.studio.contracts import (
    AgentSpec,
    BundleManifest,
    FileEntry,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.framework_run import FrameworkRunSpecResolver
from ksadk.studio.service import StudioService


def test_bundle_security_allows_secret_references_and_normal_source(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "resolved-agent-spec.json").write_text(
        json.dumps({"model": {"credentialRef": "env://MODEL_API_KEY"}}),
        encoding="utf-8",
    )
    runtime = root / "runtime"
    runtime.mkdir()
    (runtime / "agent.py").write_text("token = env.get('MODEL_API_KEY')\n", encoding="utf-8")

    assert_bundle_security(root)


@pytest.mark.parametrize(
    ("payload", "expected_kind"),
    [
        ({"model": {"apiKey": "literal-key"}}, "literal-secret-field"),
        ({"model": {"endpoint": "https://user:secret@example.test/v1"}}, "url-credentials"),
        ({"runtime": {"projectPath": "/Users/example/private-agent"}}, "local-home-path"),
    ],
)
def test_bundle_security_rejects_literal_deployment_configuration(
    tmp_path: Path,
    payload: dict[str, object],
    expected_kind: str,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "resolved-agent-spec.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BundleSecurityError) as captured:
        assert_bundle_security(root)

    assert {finding.kind for finding in captured.value.findings} == {expected_kind}


def test_bundle_security_allows_local_paths_in_packaged_skill_reference_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    reference = root / "capabilities/skills/design/references/upstream-sync/provenance.json"
    reference.parent.mkdir(parents=True)
    reference.write_text(
        json.dumps({"promptBundle": "/Users/example/source/design-system"}),
        encoding="utf-8",
    )

    assert_bundle_security(root)

    (reference.parent / "credential.json").write_text(
        json.dumps({"apiKey": "literal-key"}),
        encoding="utf-8",
    )
    with pytest.raises(BundleSecurityError) as captured:
        assert_bundle_security(root)
    assert {finding.kind for finding in captured.value.findings} == {"literal-secret-field"}


def test_bundle_security_rejects_high_confidence_runtime_key(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "agent.py").write_text(
        "api_key = 'sk-test-secret-placeholder'\n",
        encoding="utf-8",
    )

    with pytest.raises(BundleSecurityError) as captured:
        assert_bundle_security(root)

    assert captured.value.findings[0].kind == "openai-style-key"


def _new_langgraph_draft(tmp_path: Path):
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="bundle-security",
        name="Bundle Security",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/bundle-security/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://OPENAI_API_KEY",
            ),
            instructions=Instructions(system="Answer with evidence."),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    return studio, draft


def _refresh_manifest(bundle: Path) -> str:
    """Model an integrity-valid restored artifact without changing its Build id."""

    checksum_paths = sorted(
        (path for path in bundle.rglob("*") if path.is_file() and path.name != "manifest.json"),
        key=lambda path: path.relative_to(bundle).as_posix(),
    )
    (bundle / "checksums.txt").write_text(
        "\n".join(
            (
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                f"{path.relative_to(bundle).as_posix()}"
            )
            for path in checksum_paths
            if path.name != "checksums.txt"
        )
        + "\n",
        encoding="utf-8",
    )
    payload = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    payload["files"] = [
        FileEntry(
            path=path.relative_to(bundle).as_posix(),
            sha256=f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            size=len(path.read_bytes()),
        ).model_dump(by_alias=True)
        for path in sorted(
            (path for path in bundle.rglob("*") if path.is_file() and path.name != "manifest.json"),
            key=lambda path: path.relative_to(bundle).as_posix(),
        )
    ]
    manifest = BundleManifest.model_validate(payload)
    manifest.bundle_digest = compute_bundle_digest(manifest)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest.model_dump(by_alias=True, exclude_none=True)),
        encoding="utf-8",
    )
    return manifest.bundle_digest


def test_builder_rejects_secret_in_runtime_snapshot(tmp_path: Path) -> None:
    studio, draft = _new_langgraph_draft(tmp_path)
    source = studio.workspace.resolve("agents/bundle-security/source")
    source.mkdir(parents=True, exist_ok=True)
    (source / "agent.py").write_text(
        "api_key = 'sk-test-secret-placeholder'\n",
        encoding="utf-8",
    )
    with pytest.raises(BundleSecurityError, match="literal secret"):
        studio.builder.build(draft)
    dist = tmp_path / "dist"
    assert not dist.exists() or not list(dist.rglob("agent-bundle.zip"))


def test_studio_build_reports_the_rejected_bundle_file(tmp_path: Path) -> None:
    studio, draft = _new_langgraph_draft(tmp_path)
    source = studio.workspace.resolve("agents/bundle-security/source")
    source.mkdir(parents=True, exist_ok=True)
    (source / "agent.py").write_text(
        "api_key = 'sk-test-secret-placeholder'\n",
        encoding="utf-8",
    )

    with pytest.raises(StudioError) as captured:
        studio._build_agent_bundle(draft)

    assert captured.value.code == "BUNDLE_SECURITY_REJECTED"
    assert "runtime/agent.py" in captured.value.message
    assert "sk-test-secret-placeholder" not in captured.value.message


def test_v2_resolver_rechecks_security_without_changing_v1_compatibility(tmp_path: Path) -> None:
    studio, draft = _new_langgraph_draft(tmp_path)
    source = studio.workspace.resolve("agents/bundle-security/source")
    source.mkdir(parents=True, exist_ok=True)
    # Framework detection recognizes the graph variable. No literal key is
    # present during build; the post-build mutation below models a restored
    # artifact and relies on the existing integrity check being bypassed only
    # in this focused security-unit test.
    (source / "agent.py").write_text("graph = object()\n", encoding="utf-8")
    build = studio.builder.build(draft)
    bundle = studio.workspace.resolve(build.artifact_path, must_exist=True).parent / "agent-bundle"
    resolved = bundle / "resolved-agent-spec.json"
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    payload["model"]["apiKey"] = "literal-key"
    resolved.write_text(json.dumps(payload), encoding="utf-8")
    build.bundle_digest = _refresh_manifest(bundle)
    studio.builds.save(build)

    resolver = FrameworkRunSpecResolver(studio.workspace, build_repository=studio.builds)
    with pytest.raises(StudioError, match="明文凭证") as captured:
        resolver.resolve(build.id)
    assert captured.value.details["securityFindings"][0]["kind"] == "literal-secret-field"
