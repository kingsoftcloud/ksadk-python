from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest

EXPECTED_SHA256 = "b95f0735357fcf879bd53ed85cb242679ec74438e3bc8e85b1f27193169b6ecf"


def test_web_artifacts_builder_zip_matches_skill_service_fixture_contract():
    fixture_env = os.environ.get("KSADK_WEB_ARTIFACTS_FIXTURE", "").strip()
    if not fixture_env:
        pytest.skip(
            "KSADK_WEB_ARTIFACTS_FIXTURE not set; local/preprod fixture zip "
            "is not present in CI or clean environments"
        )
    fixture = Path(fixture_env)
    if not fixture.exists():
        pytest.skip(f"fixture zip not found at {fixture}")
    data = fixture.read_bytes()
    assert hashlib.sha256(data).hexdigest() == EXPECTED_SHA256

    with zipfile.ZipFile(fixture) as archive:
        names = set(archive.namelist())
        skill_md = archive.read("web-artifacts-builder/SKILL.md").decode("utf-8")

    assert "web-artifacts-builder/SKILL.md" in names
    assert "web-artifacts-builder/scripts/init-artifact.sh" in names
    assert "web-artifacts-builder/scripts/bundle-artifact.sh" in names
    assert "name: web-artifacts-builder" in skill_md
