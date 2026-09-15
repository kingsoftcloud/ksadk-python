from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ksadk.studio.workspace_registry import (
    LinkedDirectoryPolicy,
    WorkspaceRegistry,
    WorkspaceRuntimeManager,
)


def test_registry_assigns_stable_identity_and_tracks_recent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "project"
    root.mkdir()
    registry = WorkspaceRegistry(home)
    first = registry.open(root)
    second = registry.open(root / ".")
    assert first.workspace_id == second.workspace_id
    assert registry.list()[0].path == str(root.resolve())
    assert (home / "workspaces.json").stat().st_mode & 0o077 == 0


def test_concurrent_registry_instances_preserve_every_workspace(tmp_path: Path) -> None:
    home = tmp_path / "registry"
    roots = [tmp_path / f"project-{index}" for index in range(24)]
    for root in roots:
        root.mkdir()

    def open_project(root):
        return WorkspaceRegistry(home).open(root)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(open_project, roots))
    assert {record.path for record in WorkspaceRegistry(home).list()} == {
        str(root.resolve()) for root in roots
    }
    assert not list(home.glob(".workspaces-*"))


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


def test_linked_directory_policy_authorizes_read_and_explicit_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    linked = tmp_path / "linked"
    workspace.mkdir()
    linked.mkdir()
    policy = LinkedDirectoryPolicy(workspace)
    policy.set(linked, "read")
    assert policy.authorize(workspace / "agent.py", "write")
    assert policy.authorize(linked / "source.py", "read")
    assert not policy.authorize(linked / "source.py", "write")
    policy.set(linked, "write")
    assert policy.authorize(linked / "source.py", "write")
    assert not policy.authorize(tmp_path / "outside.txt", "read")


def test_runtime_manager_keeps_isolated_services(tmp_path: Path) -> None:
    class FakeService:
        def __init__(self, root):
            self.workspace = type("Workspace", (), {"root": Path(root)})()
            self.value = Path(root).name
            self.event_store = type(
                "Events",
                (),
                {
                    "list_runs": lambda self: [{"runId": "r"}],
                    "get": lambda self, _run_id: {"runId": "r"},
                    "list_traces_page": lambda self, **_: {"items": [{"traceId": "t"}]},
                },
            )()
            self.workspace_record = type("Record", (), {"workspace_id": self.value})()
            self.operations = type(
                "Operations",
                (),
                {
                    "list": lambda self: [],
                    "get": lambda self, operation_id: (
                        {"id": operation_id}
                        if operation_id == self.owner
                        else (_ for _ in ()).throw(KeyError(operation_id))
                    ),
                },
            )()
            self.operations.owner = self.value

    first = FakeService(tmp_path / "a")
    manager = WorkspaceRuntimeManager(first, FakeService)
    (tmp_path / "b").mkdir()
    record = manager.switch(tmp_path / "b")
    assert manager.value == "b"
    assert len(manager.services()) == 2
    assert manager.services()[0] is first
    assert record.path.endswith("/b")
    assert {item["workspaceId"] for item in manager.all_runs()} == {"a", "b"}
    assert manager.runtime_for_run("r") is not None
    assert {item["workspaceId"] for item in manager.all_traces()} == {"a", "b"}
    assert manager.runtime_for_operation("b") is manager.services()[1]
