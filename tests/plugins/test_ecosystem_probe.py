from __future__ import annotations

import json
from pathlib import Path

import pytest

from ksadk.plugins.ecosystem_probe import EcosystemProbeError, probe_ecosystem_manifests


def _json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_detects_codex_and_dsh_without_auto_selecting(tmp_path: Path) -> None:
    _json(tmp_path / ".codex-plugin/plugin.json", {"name": "fixture", "version": "1.0.0"})
    _json(
        tmp_path / "package.json",
        {"name": "fixture", "version": "1.0.0", "dsh": {"bundle": {"patch": "x.yml"}}},
    )

    exchange = probe_ecosystem_manifests(tmp_path)
    assert exchange.result.selection_required is True
    assert exchange.result.selected_manifest_ref is None
    assert {item.ecosystem for item in exchange.result.candidates} == {"codex", "dsh"}
    assert {item.integration_mode for item in exchange.result.candidates} == {
        "bridged",
        "linked",
    }

    codex_ref = next(
        item.manifest_ref for item in exchange.result.candidates if item.ecosystem == "codex"
    )
    selected = probe_ecosystem_manifests(tmp_path, selected_manifest_ref=codex_ref)
    assert selected.result.selection_required is False
    assert selected.result.selected_manifest_ref == codex_ref


def test_unpublished_ksadk_manifest_is_not_a_supported_ecosystem(tmp_path: Path) -> None:
    (tmp_path / "ksadk-plugin.yaml").write_text("apiVersion: plugin.ksadk.io/v1\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fixture"\nversion="1.0.0"\n'
        '[project.entry-points."ksadk.plugin"]\nfixture="fixture:create"\n'
    )
    exchange = probe_ecosystem_manifests(tmp_path)
    assert exchange.result.candidates == ()
    assert exchange.result.rejection is not None
    assert exchange.result.rejection.code == "unsupported"


def test_plain_skill_marketplace_or_package_is_not_promoted_to_plugin(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("# skill")
    _json(tmp_path / ".agents/plugins/marketplace.json", {"name": "catalog", "plugins": []})
    _json(tmp_path / "package.json", {"name": "ordinary-package", "version": "1.0.0"})
    exchange = probe_ecosystem_manifests(tmp_path)
    assert exchange.result.candidates == ()
    assert exchange.result.rejection is not None
    assert exchange.result.rejection.code == "unsupported"


def test_invalid_or_symlinked_manifest_fails_without_execution(tmp_path: Path) -> None:
    codex = tmp_path / ".codex-plugin/plugin.json"
    codex.parent.mkdir()
    codex.write_text("not-json")
    with pytest.raises(EcosystemProbeError, match="valid UTF-8 JSON"):
        probe_ecosystem_manifests(tmp_path)

    codex.unlink()
    target = tmp_path / "outside.json"
    target.write_text('{"name":"outside"}')
    codex.symlink_to(target)
    with pytest.raises(EcosystemProbeError, match="symlinks"):
        probe_ecosystem_manifests(tmp_path)


def test_symlinked_manifest_directory_cannot_escape_source_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    _json(outside / "plugin.json", {"name": "outside"})
    (tmp_path / ".codex-plugin").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EcosystemProbeError, match="must not contain symlinks"):
        probe_ecosystem_manifests(tmp_path)


def test_selected_manifest_must_exist_in_detected_candidates(tmp_path: Path) -> None:
    _json(tmp_path / ".codex-plugin/plugin.json", {"name": "fixture"})
    with pytest.raises(EcosystemProbeError, match="was not detected"):
        probe_ecosystem_manifests(
            tmp_path,
            selected_manifest_ref=(tmp_path / ".claude-plugin/plugin.json").resolve().as_uri(),
        )
