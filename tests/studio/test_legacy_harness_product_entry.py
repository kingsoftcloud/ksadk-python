"""Studio Build -> Run wiring for the narrow legacy Harness compatibility seam."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.plugins.providers.legacy import LegacyHarnessSource
from ksadk.studio.capabilities import compute_bundle_digest
from ksadk.studio.contracts import BuildRecord, BuildStatus, BundleManifest, FileEntry
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService


class _Reasoner:
    async def complete(self, *, model, prompt, messages, tools):  # noqa: ANN001
        del tools
        assert model == "fixture-model"
        assert prompt == "Preserve the historical Harness role."
        return HarnessReasoningTurn(final_text=f"legacy:{messages[-1]['content']}")


def _install_bundle(root: Path, *, bundle_format: str) -> tuple[BuildRecord, BundleManifest]:
    build_id = f"build-{bundle_format.rsplit('/', 1)[-1]}"
    artifact_dir = root / ".agentkit" / "builds" / build_id
    bundle_root = artifact_dir / "agent-bundle"
    bundle_root.mkdir(parents=True)
    artifact = artifact_dir / "agent-bundle.zip"
    artifact.write_bytes(b"fixture archive identity")
    resolved = {
        "model": {
            "provider": "openai-compatible",
            "model": "fixture-model",
            "endpointUrl": "https://model.example.test/v1/chat/completions",
            "credentialRef": "env://MODEL_API_KEY",
            "parameters": {},
        },
        "instructions": {"system": "Preserve the historical Harness role."},
        "execution": {"strategy": "direct"},
        "security": {"allowedPermissions": []},
    }
    spec = json.dumps(resolved, sort_keys=True).encode("utf-8")
    (bundle_root / "resolved-agent-spec.json").write_bytes(spec)
    manifest = BundleManifest.model_validate(
        {
            "bundleFormat": bundle_format,
            "agentId": "historical-harness",
            "sourceRevision": 3,
            "resolvedDigest": "sha256:" + "a" * 64,
            "runtimeType": "harness",
            "files": [
                FileEntry(
                    path="resolved-agent-spec.json",
                    sha256=f"sha256:{hashlib.sha256(spec).hexdigest()}",
                    size=len(spec),
                ).model_dump(by_alias=True)
            ],
        }
    )
    manifest.bundle_digest = compute_bundle_digest(manifest)
    (bundle_root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(by_alias=True, exclude_none=True, mode="json")),
        encoding="utf-8",
    )
    build = BuildRecord(
        id=build_id,
        agent_id=manifest.agent_id,
        source_revision=manifest.source_revision,
        status=BuildStatus.SUCCEEDED,
        resolved_digest=manifest.resolved_digest,
        runtime_type="harness",
        runtime_lock={"model": "fixture-model", "models": ["fixture-model"]},
        bundle_digest=manifest.bundle_digest,
        artifact_path=artifact.relative_to(root).as_posix(),
    )
    return build, manifest


@pytest.mark.asyncio
async def test_studio_product_entry_runs_only_explicit_historical_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KSADK_DSH_HOME", str(tmp_path / "no-dsh-profile"))
    build, manifest = _install_bundle(tmp_path, bundle_format="agentkit.bundle/v1")
    source = LegacyHarnessSource.from_verified_manifest(manifest)
    studio = StudioService(
        tmp_path,
        harness_reasoner=_Reasoner(),
        legacy_harness_sources=[source],
    )
    studio.builds.save(build)

    spec = studio.resolve_run_spec(build.id)
    assert spec.launch_context.runtime_type == "harness"
    assert spec.plugin_bundle_root is not None
    completed = await studio.run_build(build.id, "hello", "legacy-session")
    assert completed.output == "legacy:hello", completed.error
    await studio.aclose()


def test_studio_product_entry_rejects_unknown_v1_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KSADK_DSH_HOME", str(tmp_path / "no-dsh-profile"))
    build, _manifest = _install_bundle(tmp_path, bundle_format="agentkit.bundle/v1")
    studio = StudioService(tmp_path, harness_reasoner=_Reasoner())
    studio.builds.save(build)

    with pytest.raises(StudioError) as captured:
        studio.resolve_run_spec(build.id)
    assert captured.value.code == "PLUGIN_BUNDLE_INVALID"
    assert captured.value.details["reason"] == "legacy_harness_source_unrecognized"


def test_studio_product_entry_rejects_v2_without_dsh_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KSADK_DSH_HOME", str(tmp_path / "no-dsh-profile"))
    build, manifest = _install_bundle(tmp_path, bundle_format="agentkit.bundle/v2")
    # Even injecting the v2 digest into the legacy source registry cannot make
    # Studio use the in-process adapter for a new Bundle.
    source = LegacyHarnessSource(
        bundle_digest=manifest.bundle_digest,
        resolved_digest=manifest.resolved_digest,
        source_revision=manifest.source_revision,
    )
    studio = StudioService(
        tmp_path,
        harness_reasoner=_Reasoner(),
        legacy_harness_sources=[source],
    )
    studio.builds.save(build)

    with pytest.raises(StudioError) as captured:
        studio.resolve_run_spec(build.id)
    assert captured.value.code == "AGENT_PROVIDER_NOT_REGISTERED"
    assert captured.value.details["reason"] == "agent_provider_not_registered"
