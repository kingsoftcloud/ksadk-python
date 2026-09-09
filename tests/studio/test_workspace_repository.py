from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ksadk.studio.contracts import AgentSpec, BuildRecord, BuildStatus
from ksadk.studio.errors import StudioError
from ksadk.studio.repository import AgentDraftRepository, BuildRepository
from ksadk.studio.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    value = Workspace(tmp_path / "workspace")
    value.initialize()
    return value


def test_workspace_initializes_git_friendly_layout(workspace: Workspace):
    assert (workspace.root / "agentkit.yaml").is_file()
    assert (workspace.root / "agents").is_dir()
    assert (workspace.root / "capabilities/skills").is_dir()
    assert (workspace.root / ".agentkit/builds").is_dir()
    assert (workspace.root / "dist").is_dir()


def test_workspace_rejects_parent_path_escape(workspace: Workspace):
    with pytest.raises(StudioError) as captured:
        workspace.resolve("../outside.txt")

    assert captured.value.code == "WORKSPACE_PATH_FORBIDDEN"


def test_workspace_rejects_absolute_sibling_prefix_escape(
    workspace: Workspace, tmp_path: Path
):
    sibling = tmp_path / f"{workspace.root.name}-outside"

    with pytest.raises(StudioError) as captured:
        workspace.resolve(sibling / "secret.txt")

    assert captured.value.code == "WORKSPACE_PATH_FORBIDDEN"


def test_workspace_rejects_symlink_escape(workspace: Workspace, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StudioError) as captured:
        workspace.resolve("linked/secret.txt")

    assert captured.value.code == "WORKSPACE_PATH_FORBIDDEN"


def test_atomic_write_rejects_symlink_escape(workspace: Workspace, tmp_path: Path):
    outside = tmp_path / "outside-write"
    outside.mkdir()
    (workspace.root / "linked-write").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StudioError) as captured:
        workspace.atomic_write_text("linked-write/secret.txt", "sensitive\n")

    assert captured.value.code == "WORKSPACE_PATH_FORBIDDEN"
    assert not (outside / "secret.txt").exists()


def test_atomic_write_replaces_complete_file(workspace: Workspace):
    target = workspace.resolve("agentkit.yaml")
    workspace.atomic_write_text(target, "first\n")
    workspace.atomic_write_text(target, "second\n")

    assert target.read_text(encoding="utf-8") == "second\n"
    assert not list(target.parent.glob(".agentkit.yaml.*.tmp"))


def test_draft_create_update_list_and_revision_conflict(workspace: Workspace):
    repository = AgentDraftRepository(workspace)
    created = repository.create(
        agent_id="demo-agent",
        name="Demo Agent",
        description="First revision",
    )
    updated_spec = AgentSpec.model_validate(
        {
            **created.spec.model_dump(by_alias=True, mode="json"),
            "description": "Second revision",
        }
    )

    updated = repository.update("demo-agent", updated_spec, expected_revision=1)

    assert updated.metadata.revision == 2
    assert repository.get("demo-agent").spec.description == "Second revision"
    assert [item.metadata.id for item in repository.list(query="Demo")] == ["demo-agent"]

    with pytest.raises(StudioError) as captured:
        repository.update("demo-agent", updated_spec, expected_revision=1)

    assert captured.value.code == "AGENT_REVISION_CONFLICT"
    assert repository.get("demo-agent").metadata.revision == 2


def test_draft_duplicate_and_soft_delete(workspace: Workspace):
    repository = AgentDraftRepository(workspace)
    repository.create(agent_id="demo-agent", name="Demo Agent")

    with pytest.raises(StudioError) as captured:
        repository.create(agent_id="demo-agent", name="Other")
    assert captured.value.code == "AGENT_ALREADY_EXISTS"

    repository.delete("demo-agent")

    with pytest.raises(StudioError) as missing:
        repository.get("demo-agent")
    assert missing.value.code == "AGENT_NOT_FOUND"
    assert list((workspace.root / ".agentkit/trash").glob("demo-agent-*"))


def test_build_repository_round_trip(workspace: Workspace):
    repository = BuildRepository(workspace)
    record = BuildRecord(
        id="build_123",
        agent_id="demo-agent",
        source_revision=1,
        status=BuildStatus.SUCCEEDED,
        resolved_digest="sha256:resolved",
        bundle_digest="sha256:bundle",
        artifact_path="dist/demo-agent/build_123/agent-bundle.zip",
    )

    repository.save(record)

    loaded = repository.get("build_123")
    assert loaded == record
    assert repository.list_for_agent("demo-agent") == [record]


def test_build_repository_lists_newest_build_first(workspace: Workspace):
    repository = BuildRepository(workspace)
    now = datetime.now(timezone.utc)
    older = BuildRecord(
        id="build_z_old",
        agent_id="demo-agent",
        source_revision=1,
        status=BuildStatus.SUCCEEDED,
        created_at=now - timedelta(minutes=1),
    )
    newer = BuildRecord(
        id="build_a_new",
        agent_id="demo-agent",
        source_revision=2,
        status=BuildStatus.SUCCEEDED,
        created_at=now,
    )
    repository.save(older)
    repository.save(newer)

    assert repository.list_for_agent("demo-agent") == [newer, older]
