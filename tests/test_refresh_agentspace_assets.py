from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "openclaw"
    / "scripts"
    / "refresh_agentspace_assets.py"
)
SPEC = importlib.util.spec_from_file_location("refresh_agentspace_assets", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_safe_extract_tar_rejects_path_traversal(tmp_path: Path):
    archive_path = tmp_path / "bad.tar.gz"
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()

    with tarfile.open(archive_path, "w:gz") as tf:
        payload = b"owned"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    with pytest.raises(RuntimeError, match="escapes target dir"):
        MODULE._safe_extract_tar(archive_path, extract_dir)


def test_safe_extract_zip_rejects_path_traversal(tmp_path: Path):
    archive_path = tmp_path / "bad.zip"
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()

    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("../escape.txt", "owned")

    with pytest.raises(RuntimeError, match="escapes target dir"):
        MODULE._safe_extract_zip(archive_path, extract_dir)


def test_safe_extract_tar_allows_regular_entries(tmp_path: Path):
    archive_path = tmp_path / "ok.tar.gz"
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()

    with tarfile.open(archive_path, "w:gz") as tf:
        payload = b"hello"
        info = tarfile.TarInfo("package/SKILL.md")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    MODULE._safe_extract_tar(archive_path, extract_dir)

    assert (extract_dir / "package" / "SKILL.md").read_bytes() == b"hello"


def _build_skill_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("wps365-skill/SKILL.md", "skill root")
        zf.writestr("wps365-skill/requirements.txt", "requests==2.32.0\n")
        zf.writestr("wps365-skill/skills/demo/SKILL.md", "demo")
    return buffer.getvalue()


def test_extract_wps365_skill_skips_rewrite_when_contents_match(tmp_path: Path):
    dst_skill_dir = tmp_path / "wps365-skill"

    changed = MODULE._extract_wps365_skill(_build_skill_zip_bytes(), dst_skill_dir)
    assert changed is True
    first_signature = MODULE._directory_content_signature(dst_skill_dir)

    changed = MODULE._extract_wps365_skill(_build_skill_zip_bytes(), dst_skill_dir)
    assert changed is False
    second_signature = MODULE._directory_content_signature(dst_skill_dir)

    assert first_signature == second_signature


def test_write_lock_if_changed_keeps_existing_file_when_functional_payload_matches(tmp_path: Path):
    lock_path = tmp_path / "agentspace-assets.lock.json"
    existing_payload = {
        "source": {
            "installer_entry_url": "https://agentspace.wps.cn/openclaw/plugins/installer",
            "installer_resolved_url": "https://cdn.example.com/installer.tgz",
            "installer_sha256": "aaa",
            "installer_version": "2.0.9",
        },
        "assets": {
            "agentspace_plugin": {"url": "https://cdn.example.com/plugin.tgz", "sha256": "bbb"},
            "wps365_skill": {
                "url": "https://cdn.example.com/skill.zip",
                "sha256": "ccc",
                "preset_path": "deploy/openclaw/preset-skills/wps365-skill",
            },
        },
        "refreshed_at_utc": "2026-03-26T00:00:00Z",
    }
    lock_path.write_text(json.dumps(existing_payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    before_text = lock_path.read_text("utf-8")

    changed = MODULE._write_lock_if_changed(
        lock_path,
        {
            "source": existing_payload["source"],
            "assets": existing_payload["assets"],
        },
    )

    assert changed is False
    assert lock_path.read_text("utf-8") == before_text


def test_write_lock_if_changed_rewrites_when_functional_payload_changes(tmp_path: Path):
    lock_path = tmp_path / "agentspace-assets.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "source": {
                    "installer_entry_url": "https://agentspace.wps.cn/openclaw/plugins/installer",
                    "installer_resolved_url": "https://cdn.example.com/installer.tgz",
                    "installer_sha256": "old",
                    "installer_version": "2.0.8",
                },
                "assets": {
                    "agentspace_plugin": {"url": "https://cdn.example.com/plugin-old.tgz", "sha256": "old"},
                    "wps365_skill": {
                        "url": "https://cdn.example.com/skill-old.zip",
                        "sha256": "old",
                        "preset_path": "deploy/openclaw/preset-skills/wps365-skill",
                    },
                },
                "refreshed_at_utc": "2026-03-26T00:00:00Z",
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    changed = MODULE._write_lock_if_changed(
        lock_path,
        {
            "source": {
                "installer_entry_url": "https://agentspace.wps.cn/openclaw/plugins/installer",
                "installer_resolved_url": "https://cdn.example.com/installer.tgz",
                "installer_sha256": "new",
                "installer_version": "2.0.9",
            },
            "assets": {
                "agentspace_plugin": {"url": "https://cdn.example.com/plugin-new.tgz", "sha256": "new"},
                "wps365_skill": {
                    "url": "https://cdn.example.com/skill-new.zip",
                    "sha256": "new",
                    "preset_path": "deploy/openclaw/preset-skills/wps365-skill",
                },
            },
        },
    )

    assert changed is True
    payload = json.loads(lock_path.read_text("utf-8"))
    assert payload["source"]["installer_sha256"] == "new"
    assert payload["assets"]["agentspace_plugin"]["sha256"] == "new"
    assert payload["refreshed_at_utc"]
