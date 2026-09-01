"""Build provenance must stay usable in a Git-free clean public export."""

from __future__ import annotations

import json

import pytest

from scripts.write_build_provenance import build_provenance


def test_build_provenance_uses_clean_export_manifest_without_git(tmp_path) -> None:
    (tmp_path / "export-manifest.json").write_text(
        json.dumps(
            {
                "sourceCommit": "a" * 40,
                "sourceTree": "clean",
            }
        ),
        encoding="utf-8",
    )

    provenance = build_provenance(tmp_path)

    assert provenance["sourceCommit"] == "a" * 40
    assert provenance["sourceTree"] == "clean"


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"sourceCommit": "not-a-commit", "sourceTree": "clean"},
        {"sourceCommit": "a" * 40, "sourceTree": "dirty"},
    ],
)
def test_build_provenance_rejects_untrusted_export_manifest(tmp_path, manifest) -> None:
    (tmp_path / "export-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="clean export manifest"):
        build_provenance(tmp_path)
