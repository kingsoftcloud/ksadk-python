"""Phase 1 跨仓基线 manifest 的构建与校验测试（plan Task 0）。

约束来自 docs/superpowers/plans/2026-08-17-agent-runtime-v2-phase1-agent-kernel.md：
- phase0_gate_status 必须来自 Phase 0 release manifest 的 accepted=true，不能伪造；
- 拒绝 dirty worktree、缺 commit、缺 remote、重复 repo key。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.build_phase1_baseline import (
    BaselineError,
    Phase0Gate,
    Phase1Baseline,
    RepoBaseline,
)
from scripts.verify_phase1_baseline import verify_manifest


def make_repo(**overrides) -> RepoBaseline:
    base = dict(
        repo="ksadk-python",
        remote="ssh://git@example.com/ksadk-python.git",
        branch_base="feat/runtime-event-v2-redesign",
        commit_sha="0" * 40,
        dirty=False,
    )
    base.update(overrides)
    return RepoBaseline(**base)


def make_manifest(
    *,
    phase0_gate_status: str = "accepted",
    dirty: bool = False,
    repos: list[RepoBaseline] | None = None,
) -> Phase1Baseline:
    return Phase1Baseline(
        schema_version=1,
        repositories=repos if repos is not None else [make_repo(dirty=dirty)],
        phase0=Phase0Gate(
            accepted=phase0_gate_status == "accepted",
            status=phase0_gate_status,
            manifest_path="docs/superpowers/evidence/phase0/manifest.json",
            manifest_digest="0" * 64,
        ),
        contract_digest=None,
        captured_at="2026-08-17T00:00:00Z",
    )


def write_manifest(tmp_path: Path, manifest: Phase1Baseline) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(manifest.model_dump_json(indent=2))
    return path


def test_phase1_baseline_rejects_dirty_or_unaccepted_repo(tmp_path):
    manifest = make_manifest(phase0_gate_status="failed", dirty=True)
    path = write_manifest(tmp_path, manifest)
    with pytest.raises(BaselineError, match="phase0_not_accepted|dirty_worktree"):
        verify_manifest(path)


def test_rejects_missing_commit_or_remote(tmp_path):
    manifest = make_manifest(
        repos=[make_repo(commit_sha="", remote="")]
    )
    path = write_manifest(tmp_path, manifest)
    with pytest.raises(BaselineError, match="missing_commit|missing_remote"):
        verify_manifest(path)


def test_rejects_duplicate_repo_key(tmp_path):
    manifest = make_manifest(repos=[make_repo(), make_repo()])
    path = write_manifest(tmp_path, manifest)
    with pytest.raises(BaselineError, match="duplicate_repo"):
        verify_manifest(path)


def test_accepts_valid_manifest(tmp_path):
    path = write_manifest(tmp_path, make_manifest())
    verify_manifest(path)


def test_baseline_matches_real_git_facts(tmp_path):
    """build_manifest 必须读真实 git 事实，不能由参数伪造。"""
    from scripts.build_phase1_baseline import build_manifest

    repo_root = Path(__file__).resolve().parents[2]
    baseline = build_manifest({"ksadk-python": repo_root})
    repo = baseline.repositories[0]
    head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert repo.commit_sha == head
    assert len(repo.commit_sha) == 40


def test_phase0_manifest_missing_is_not_accepted(tmp_path):
    from scripts.build_phase1_baseline import load_phase0_gate

    gate = load_phase0_gate(tmp_path)
    assert gate.accepted is False
    assert gate.status == "phase0_manifest_missing"


def test_phase0_manifest_accepted_round_trip(tmp_path):
    from scripts.build_phase1_baseline import load_phase0_gate

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"accepted": True, "digest": "0" * 64}))
    gate = load_phase0_gate(tmp_path)
    assert gate.accepted is True
