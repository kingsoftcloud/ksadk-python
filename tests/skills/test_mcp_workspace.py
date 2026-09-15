from __future__ import annotations

import os
import threading
import time

import pytest

from ksadk.skills import manifest_cache
from ksadk.skills.manifest_cache import ManifestItem
from ksadk.skills.mcp_server import _registry, register, server

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Hosted workspace uses POSIX files")

MARKER = ".ksadk-skill-center"
BLOCK = "## Available Skills (Skill Center)\nExample catalogue\n"


def _item(name="example"):
    return ManifestItem(name=name, description="Example", version="1", space_id="space-example")


@pytest.fixture(params=["hermes", "openclaw"])
def hub(request, monkeypatch, tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv(f"{request.param.upper()}_SKILL_HUB_DIR", str(root))
    return root, getattr(register, f"_inject_{request.param}_skill_hub")


@pytest.mark.parametrize("kind", ["directory", "instructions", "marker"])
@pytest.mark.parametrize("outside", [True, False])
def test_refresh_preserves_symlink_targets(hub, tmp_path, kind, outside):
    root, inject = hub
    destination = (tmp_path if outside else root) / ".user-data"
    destination.mkdir()
    (destination / MARKER).write_text("1")
    target = destination / "SKILL.md"
    target.write_text("USER INSTRUCTIONS")
    skill = root / "example"
    if kind == "directory":
        skill.symlink_to(destination, target_is_directory=True)
    else:
        skill.mkdir()
        (skill / MARKER).write_text("1")
        (skill / "SKILL.md").write_text("OLD INSTRUCTIONS")
        link = skill / (MARKER if kind == "marker" else "SKILL.md")
        link.unlink()
        link.symlink_to(target)
    inject(BLOCK, [_item()])
    assert target.read_text() == "USER INSTRUCTIONS"
    assert (skill if kind == "directory" else link).is_symlink()


@pytest.mark.parametrize("leaf", [MARKER, "SKILL.md"])
def test_refresh_does_not_follow_dangling_links(hub, tmp_path, leaf):
    root, inject = hub
    skill = root / "example"
    skill.mkdir()
    (skill / MARKER).write_text("1")
    link = skill / leaf
    link.unlink(missing_ok=True)
    target = tmp_path / "must-not-be-created"
    link.symlink_to(target)
    inject(BLOCK, [_item()])
    assert not target.exists()
    assert link.is_symlink()


@pytest.mark.parametrize("kind", ["directory", "marker", "instructions"])
def test_cleanup_rejects_symlink_ownership_and_paths(hub, tmp_path, kind):
    root, inject = hub
    destination = tmp_path / "user-data"
    destination.mkdir()
    (destination / MARKER).write_text("1")
    (destination / "SKILL.md").write_text("USER INSTRUCTIONS")
    skill = root / "example"
    if kind == "directory":
        skill.symlink_to(destination, target_is_directory=True)
    else:
        skill.mkdir()
        (skill / MARKER).write_text("1")
        (skill / "SKILL.md").write_text("USER LOCAL INSTRUCTIONS")
        link = skill / (MARKER if kind == "marker" else "SKILL.md")
        link.unlink()
        link.symlink_to(destination / "SKILL.md")
    inject("", [])
    assert skill.exists()
    assert (destination / "SKILL.md").read_text() == "USER INSTRUCTIONS"
    if kind == "marker":
        assert (skill / "SKILL.md").read_text() == "USER LOCAL INSTRUCTIONS"


def test_cleanup_removes_generated_files_but_preserves_user_additions(hub):
    root, inject = hub
    inject(BLOCK, [_item(), _item("clean")])
    helper = root / "example" / "notes.txt"
    helper.write_text("USER NOTES")
    inject("", [])
    assert not (root / "clean").exists()
    assert helper.read_text() == "USER NOTES"
    assert not (root / "example" / "SKILL.md").exists()
    assert (root / "example" / MARKER).is_file()
    inject(BLOCK, [_item()])
    assert (root / "example" / "SKILL.md").is_file()
    assert helper.read_text() == "USER NOTES"


def test_existing_local_skill_is_never_adopted(hub):
    root, inject = hub
    skill = root / "example"
    skill.mkdir()
    (skill / "SKILL.md").write_text("USER LOCAL INSTRUCTIONS")
    inject(BLOCK, [_item()])
    inject("", [])
    assert (skill / "SKILL.md").read_text() == "USER LOCAL INSTRUCTIONS"
    assert not (skill / MARKER).exists()


@pytest.mark.parametrize(
    "initial",
    [
        "# User prefix\n" + BLOCK + "\n## User preferences\nKEEP ME\n",
        "# User prefix\n<!-- skill-center-start -->\nPartial block\nKEEP ME\n",
    ],
)
def test_legacy_or_incomplete_tools_block_is_preserved(monkeypatch, tmp_path, initial):
    tools = tmp_path / "TOOLS.md"
    tools.write_text(initial)
    monkeypatch.setenv("OPENCLAW_TOOLS_MD", str(tools))
    register._inject_openclaw_workspace(BLOCK)
    assert initial in tools.read_text()
    register._inject_openclaw_workspace("")
    assert initial in tools.read_text()


def test_tools_refresh_then_clear_preserves_user_sections(monkeypatch, tmp_path):
    tools = tmp_path / "TOOLS.md"
    prefix = "# User prefix\n"
    suffix = "\n## User preferences\nKEEP ME\n"
    tools.write_text(prefix)
    monkeypatch.setenv("OPENCLAW_TOOLS_MD", str(tools))
    register._inject_openclaw_workspace(BLOCK)
    tools.write_text(tools.read_text() + suffix)
    register._inject_openclaw_workspace(BLOCK + "Updated catalogue\n")
    updated = tools.read_text()
    assert prefix in updated and suffix in updated
    assert updated.count("<!-- skill-center-start -->") == 1
    assert "Updated catalogue" in updated
    register._inject_openclaw_workspace("")
    assert prefix in tools.read_text() and suffix in tools.read_text()
    assert "Available Skills" not in tools.read_text()


def test_tools_md_symlink_is_not_written(monkeypatch, tmp_path):
    original = tmp_path / "user.md"
    original.write_text("USER NOTES")
    tools = tmp_path / "TOOLS.md"
    tools.symlink_to(original)
    monkeypatch.setenv("OPENCLAW_TOOLS_MD", str(tools))
    register._inject_openclaw_workspace(BLOCK)
    assert tools.is_symlink()
    assert original.read_text() == "USER NOTES"


def test_tools_md_permissions_are_preserved(monkeypatch, tmp_path):
    tools = tmp_path / "TOOLS.md"
    tools.write_text("USER NOTES")
    tools.chmod(0o600)
    monkeypatch.setenv("OPENCLAW_TOOLS_MD", str(tools))
    register._inject_openclaw_workspace(BLOCK)
    assert tools.stat().st_mode & 0o777 == 0o600


def test_refresh_cannot_follow_a_link_swapped_at_the_write_boundary(hub, tmp_path, monkeypatch):
    root, inject = hub
    inject(BLOCK, [_item()])
    target = tmp_path / "user.md"
    target.write_text("USER NOTES")
    original_replace = os.replace
    swapped = False

    def swap_then_replace(src, dst, **kwargs):
        nonlocal swapped
        if dst == "SKILL.md":
            leaf = root / "example" / "SKILL.md"
            leaf.unlink()
            leaf.symlink_to(target)
            swapped = True
        return original_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", swap_then_replace)
    inject(BLOCK, [_item()])
    assert swapped
    assert target.read_text() == "USER NOTES"
    assert "load_skill" in (root / "example" / "SKILL.md").read_text()


@pytest.mark.parametrize("suffix", ["", "/", "/."])
def test_hub_root_symlink_is_not_used(hub, tmp_path, monkeypatch, suffix):
    root, inject = hub
    target = tmp_path / "user-directory"
    target.mkdir()
    root.rmdir()
    root.symlink_to(target, target_is_directory=True)
    for host in ("hermes", "openclaw"):
        monkeypatch.setenv(f"{host.upper()}_SKILL_HUB_DIR", str(root) + suffix)
    inject(BLOCK, [_item()])
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("name", ["../escape", "/absolute", "nested/child", "nested\\child", "."])
def test_unsafe_names_do_not_create_entries(hub, name):
    root, inject = hub
    inject(BLOCK, [_item(name)])
    assert list(root.iterdir()) == []


def test_successful_empty_refresh_removes_the_last_skill(monkeypatch, tmp_path):
    roots = [tmp_path / host for host in ("hermes", "openclaw")]
    for host, root in zip(("hermes", "openclaw"), roots):
        root.mkdir()
        monkeypatch.setenv(f"{host.upper()}_SKILL_HUB_DIR", str(root))
    tools = tmp_path / "TOOLS.md"
    tools.write_text("USER NOTES\n")
    monkeypatch.setenv("OPENCLAW_TOOLS_MD", str(tools))
    monkeypatch.setenv("SKILL_SPACE_ID", "space-example")
    monkeypatch.setattr(_registry, "_refresh_callbacks", [])
    register._register_refresh_callbacks()
    counts = []
    _registry.register_refresh_callback(lambda text, items: counts.append(len(items)))

    class Cache:
        def get_all(self, **kwargs):
            return [_item()] if not counts else []

        def build_instruction_text(self):
            return BLOCK

    class StopRefresh(BaseException):
        pass

    def stop_after_two(_):
        if len(counts) == 2:
            raise StopRefresh()

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(manifest_cache, "get_manifest_cache", Cache)
    monkeypatch.setattr(threading, "Thread", InlineThread)
    monkeypatch.setattr(time, "sleep", stop_after_two)
    with pytest.raises(StopRefresh):
        server._start_background_skill_refresh()
    assert counts == [1, 0]
    assert all(not (root / "example").exists() for root in roots)
    assert "USER NOTES" in tools.read_text()
    assert "Available Skills" not in tools.read_text()
