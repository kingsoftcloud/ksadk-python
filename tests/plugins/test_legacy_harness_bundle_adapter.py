"""Exact historical Harness compatibility and Bundle v2 fail-closed gates."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest

from ksadk.plugins.providers.legacy_catalog import KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID
from ksadk.plugins.providers.legacy import (
    LegacyBundleAdapter,
    LegacyBundleCompatibilityError,
    LegacyHarnessSource,
)
from ksadk.studio.capabilities import compute_bundle_digest
from ksadk.studio.contracts import BundleManifest


def _manifest(
    *,
    bundle_format: str = "agentkit.bundle/v1",
    runtime_type: str = "",
    resolved_digest: str = "sha256:" + "a" * 64,
    source_revision: int = 7,
) -> BundleManifest:
    manifest = BundleManifest.model_validate(
        {
            "bundleFormat": bundle_format,
            "agentId": "historical-harness",
            "sourceRevision": source_revision,
            "resolvedDigest": resolved_digest,
            "runtimeType": runtime_type,
            "files": [],
        }
    )
    manifest.bundle_digest = compute_bundle_digest(manifest)
    return manifest


def test_only_exact_registered_v1_identity_selects_legacy_harness() -> None:
    historical = _manifest()
    source = LegacyHarnessSource.from_verified_manifest(historical)
    selection = LegacyBundleAdapter([source]).select_harness_provider(historical)

    assert selection is not None
    assert selection.route == "legacy"
    assert selection.manifest is not None
    assert selection.manifest.metadata.id == KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID


def test_unknown_v1_harness_does_not_gain_legacy_provider_from_version_alone() -> None:
    claimed = _manifest(runtime_type="harness")

    with pytest.raises(LegacyBundleCompatibilityError) as captured:
        LegacyBundleAdapter().select_harness_provider(claimed)

    assert captured.value.code == "legacy_harness_source_unrecognized"


def test_registered_digest_rejects_tampered_historical_manifest() -> None:
    historical = _manifest()
    source = LegacyHarnessSource.from_verified_manifest(historical)
    historical.resolved_digest = "sha256:" + "b" * 64

    with pytest.raises(LegacyBundleCompatibilityError) as captured:
        LegacyBundleAdapter([source]).select_harness_provider(historical)

    assert captured.value.code == "legacy_harness_bundle_digest_mismatch"


def test_new_v2_harness_never_falls_back_to_legacy_allowlist() -> None:
    old = _manifest()
    source = LegacyHarnessSource.from_verified_manifest(old)
    current = _manifest(bundle_format="agentkit.bundle/v2", runtime_type="harness")
    # Even an incorrectly configured allowlist entry for the v2 digest cannot
    # authorize the in-process compatibility manifest.
    adapter = LegacyBundleAdapter(
        [
            source,
            LegacyHarnessSource(
                bundle_digest=current.bundle_digest,
                resolved_digest=current.resolved_digest,
                source_revision=current.source_revision,
            ),
        ]
    )

    with pytest.raises(LegacyBundleCompatibilityError) as captured:
        adapter.select_harness_provider(current)
    assert captured.value.code == "agent_provider_not_registered"

    selection = adapter.select_harness_provider(
        current,
        registered_provider_ids=[KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID],
    )
    assert selection is not None
    assert selection.route == "dsh"
    assert selection.manifest is None


def test_new_composed_v2_harness_carries_an_explicit_composition_mode() -> None:
    manifest = _manifest(bundle_format="agentkit.bundle/v2", runtime_type="harness")
    # A historical v2 artifact has no discriminator and normalizes as legacy.
    assert manifest.execution_profile == "legacy"

    composed = manifest.model_copy(
        update={
            "composition_mode": "composed",
            "composition_profile_digest": "sha256:" + "c" * 64,
        }
    )
    assert composed.execution_profile == "composed"


def test_real_release_082_fixture_is_v2_langgraph_and_never_legacy_harness() -> None:
    fixture_root = Path(__file__).parents[1] / "compat" / "fixtures"
    archive = base64.b64decode(
        (fixture_root / "v0.8.2-agent-bundle.zip.b64").read_text(encoding="ascii")
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        manifest = BundleManifest.model_validate(json.loads(bundle.read("manifest.json")))

    assert manifest.bundle_format == "agentkit.bundle/v2"
    assert manifest.runtime_type == "langgraph"
    assert LegacyBundleAdapter().select_harness_provider(manifest) is None


def test_established_v1_framework_bundle_stays_outside_harness_adapter() -> None:
    langgraph = _manifest(runtime_type="langgraph")

    assert LegacyBundleAdapter().select_harness_provider(langgraph) is None
