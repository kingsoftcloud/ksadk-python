from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.workspaces import prepare_workspace


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def prepare(tmp_path, source, *, attempt="attempt-one", **plan):
    return prepare_workspace(
        tmp_path / "outputs",
        group_id="group",
        team_run_id="run",
        member_id="member",
        attempt_id=attempt,
        plan={"sourcePath": str(source), **plan},
        allowed_roots=[tmp_path / "sources"],
    )


def test_git_baseline_is_frozen_and_attempt_edits_never_change_source(tmp_path):
    source = tmp_path / "sources" / "repo"
    source.mkdir(parents=True)
    git(source, "init")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "tracked.txt").write_text("baseline")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "fixture baseline")
    original = git(source, "rev-parse", "HEAD")
    original_branch = git(source, "branch", "--show-current")
    (source / "tracked.txt").write_text("uncommitted source")
    first = prepare(tmp_path, source)
    assert (first / "tracked.txt").read_text() == "baseline"
    assert git(first, "rev-parse", "HEAD") == original
    assert git(first, "branch", "--show-current") == ""
    (first / "tracked.txt").write_text("attempt result")
    assert prepare(tmp_path, source) == first
    assert (first / "tracked.txt").read_text() == "attempt result"
    assert (source / "tracked.txt").read_text() == "uncommitted source"
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "later source commit")
    second = prepare(tmp_path, source, attempt="attempt-two")
    assert (second / "tracked.txt").read_text() == "baseline"
    assert git(second, "rev-parse", "HEAD") == original
    assert git(source, "branch", "--show-current") == original_branch
    assert git(source, "status", "--porcelain") == ""


def test_directory_copies_only_explicit_frozen_inputs_and_preserves_retry_files(tmp_path):
    source = tmp_path / "sources" / "files"
    (source / "docs").mkdir(parents=True)
    (source / "docs" / "brief.txt").write_text("approved input")
    (source / "private.txt").write_text("not selected")
    first = prepare(tmp_path, source, inputs=["docs/brief.txt"])
    assert (first / "docs" / "brief.txt").read_text() == "approved input"
    assert not (first / "private.txt").exists()
    (source / "docs" / "brief.txt").write_text("changed source")
    second = prepare(tmp_path, source, attempt="attempt-two", inputs=["docs/brief.txt"])
    assert (second / "docs" / "brief.txt").read_text() == "approved input"
    (first / "result.md").write_text("work in progress")
    assert prepare(tmp_path, source, inputs=["docs/brief.txt"]) == first
    assert (first / "result.md").read_text() == "work in progress"
    with pytest.raises(TeamsError, match="已经冻结"):
        prepare(tmp_path, source, inputs=["private.txt"])


def test_workspace_rejects_ungranted_sources_and_symlink_inputs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(TeamsError, match="允许"):
        prepare(tmp_path, outside)
    source = tmp_path / "sources" / "files"
    source.mkdir(parents=True)
    (outside / "data").write_text("private")
    (source / "escape").symlink_to(outside / "data")
    with pytest.raises(TeamsError):
        prepare(tmp_path, source, inputs=["escape"])
    with pytest.raises(TeamsError):
        prepare(tmp_path, source, inputs=["../outside/data"])


def test_remote_workspace_requires_resolvable_exact_commit(tmp_path):
    source = tmp_path / "sources" / "repo"
    source.mkdir(parents=True)
    git(source, "init")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    commit = git(source, "rev-parse", "HEAD")
    common = dict(
        output_root=tmp_path / "outputs",
        group_id="group",
        team_run_id="run",
        member_id="leader",
        attempt_id="attempt",
        allowed_roots=[source],
        require_pinned_git=True,
    )
    for base in (None, "HEAD", commit[:12]):
        with pytest.raises(TeamsError) as error:
            prepare_workspace(**common, plan={"sourcePath": str(source), "baseRef": base})
        assert error.value.code == "workspace_commit_required"
    target = prepare_workspace(**common, plan={"sourcePath": str(source), "baseRef": commit})
    assert git(target, "rev-parse", "HEAD") == commit
