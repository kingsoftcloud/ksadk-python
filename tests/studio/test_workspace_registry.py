from pathlib import Path

from ksadk.studio.workspace_registry import LinkedDirectoryPolicy, WorkspaceRegistry, WorkspaceRuntimeManager


def test_registry_assigns_stable_identity_and_tracks_recent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "project"
    root.mkdir()
    registry = WorkspaceRegistry(home)
    first = registry.open(root)
    second = registry.open(root / ".")
    assert first.workspace_id == second.workspace_id
    assert registry.list()[0].path == str(root.resolve())


def test_linked_directory_policy_round_trip_and_remove(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    linked = tmp_path / "资料"
    workspace.mkdir()
    linked.mkdir()
    policy = LinkedDirectoryPolicy(workspace)
    item = policy.set(linked, "write")
    assert item.mode == "write"
    assert policy.list() == [item]
    assert policy.remove(linked)
    assert policy.list() == []


def test_runtime_manager_keeps_isolated_services(tmp_path: Path) -> None:
    class FakeService:
        def __init__(self, root):
            self.workspace = type("Workspace", (), {"root": Path(root)})()
            self.value = Path(root).name

    first = FakeService(tmp_path / "a")
    manager = WorkspaceRuntimeManager(first, FakeService)
    (tmp_path / "b").mkdir()
    record = manager.switch(tmp_path / "b")
    assert manager.value == "b"
    assert len(manager.services()) == 2
    assert manager.services()[0] is first
    assert record.path.endswith("/b")
