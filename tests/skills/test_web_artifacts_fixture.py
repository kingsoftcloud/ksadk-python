from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path


FIXTURE = Path(
    os.environ.get("KSADK_WEB_ARTIFACTS_FIXTURE", "~/Downloads/web-artifacts-builder.zip")
).expanduser()
EXPECTED_SHA256 = "b95f0735357fcf879bd53ed85cb242679ec74438e3bc8e85b1f27193169b6ecf"


def test_web_artifacts_builder_zip_matches_skill_service_fixture_contract():
    assert FIXTURE.exists(), "fixture zip should be present for local/preprod verification"
    data = FIXTURE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == EXPECTED_SHA256

    with zipfile.ZipFile(FIXTURE) as archive:
        names = set(archive.namelist())
        skill_md = archive.read("web-artifacts-builder/SKILL.md").decode("utf-8")

    assert "web-artifacts-builder/SKILL.md" in names
    assert "web-artifacts-builder/scripts/init-artifact.sh" in names
    assert "web-artifacts-builder/scripts/bundle-artifact.sh" in names
    assert "name: web-artifacts-builder" in skill_md
