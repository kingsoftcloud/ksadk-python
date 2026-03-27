#!/usr/bin/env python3
"""Refresh OpenClaw Agentspace plugin/skill assets from upstream installer.

This script resolves the latest installer from:
  https://agentspace.wps.cn/openclaw/plugins/installer

Then it extracts:
  - latest agentspace plugin tarball URL
  - latest wps365 skill zip URL

It writes:
  - deploy/openclaw/agentspace-assets.lock.json
  - deploy/openclaw/preset-skills/wps365-skill/*
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

INSTALLER_ENTRY_URL = "https://agentspace.wps.cn/openclaw/plugins/installer"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_url(url: str, timeout: int = 60) -> tuple[bytes, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ksadk-openclaw-assets-refresh/1.0",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        body = resp.read()
    return body, final_url


def _resolve_safe_archive_target(base_dir: Path, member_name: str) -> Path:
    if not member_name:
        raise RuntimeError("archive entry name is empty")
    target_path = (base_dir / member_name).resolve()
    base_resolved = base_dir.resolve()
    if os.path.commonpath([str(base_resolved), str(target_path)]) != str(base_resolved):
        raise RuntimeError(f"archive entry escapes target dir: {member_name}")
    return target_path


def _safe_extract_tar(tgz_path: Path, extract_dir: Path) -> None:
    with tarfile.open(tgz_path, "r:gz") as tf:
        members = tf.getmembers()
        for member in members:
            _resolve_safe_archive_target(extract_dir, member.name)
            if member.issym() or member.islnk():
                raise RuntimeError(f"refusing to extract tar link entry: {member.name}")
        try:
            tf.extractall(extract_dir, members=members, filter="data")
        except TypeError:
            tf.extractall(extract_dir, members=members)


def _safe_extract_zip(zip_path: Path, extract_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            _resolve_safe_archive_target(extract_dir, info.filename)
        zf.extractall(extract_dir)


def _extract_installer_urls(installer_tgz_bytes: bytes) -> tuple[str, str, str]:
    with tempfile.TemporaryDirectory(prefix="agentspace-installer-") as td:
        tgz_path = Path(td) / "installer.tgz"
        tgz_path.write_bytes(installer_tgz_bytes)
        extract_dir = Path(td) / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        _safe_extract_tar(tgz_path, extract_dir)

        package_json = extract_dir / "package" / "package.json"
        installer_version = "unknown"
        if package_json.exists():
            try:
                installer_version = json.loads(package_json.read_text("utf-8")).get("version", "unknown")
            except Exception:
                installer_version = "unknown"

        all_text = []
        for p in extract_dir.rglob("*"):
            if p.is_file() and p.suffix in {".js", ".mjs", ".cjs", ".json", ".ts"}:
                try:
                    all_text.append(p.read_text("utf-8"))
                except Exception:
                    continue
        blob = "\n".join(all_text)

        plugin_candidates = re.findall(r"https?://[^\s\"']+ecis-agentspace-[^\"'\s]+\.tgz", blob)
        skill_candidates = re.findall(r"https?://[^\s\"']+wps365-skill[^\"'\s]+\.zip", blob)

        if not plugin_candidates:
            raise RuntimeError("failed to locate agentspace plugin tarball url in installer package")
        if not skill_candidates:
            skill_name_match = re.search(r"""skillZipName\s*=\s*["']([^"']+\.zip)["']""", blob)
            if skill_name_match and plugin_candidates:
                skill_zip_name = skill_name_match.group(1).strip()
                plugin_url_for_base = plugin_candidates[-1]
                base_url = plugin_url_for_base.rsplit("/", 1)[0]
                skill_candidates = [f"{base_url}/{skill_zip_name}"]
        if not skill_candidates:
            raise RuntimeError("failed to locate wps365 skill zip url in installer package")

        plugin_url = plugin_candidates[-1]
        skill_url = skill_candidates[-1]
        return installer_version, plugin_url, skill_url


def _safe_remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _directory_content_signature(root_dir: Path) -> str:
    hasher = hashlib.sha256()
    if not root_dir.exists():
        return ""

    for file_path in sorted(path for path in root_dir.rglob("*") if path.is_file()):
        relative = file_path.relative_to(root_dir).as_posix().encode("utf-8")
        hasher.update(relative)
        hasher.update(b"\0")
        hasher.update(file_path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _sync_directory_if_changed(src_dir: Path, dst_dir: Path) -> bool:
    src_signature = _directory_content_signature(src_dir)
    dst_signature = _directory_content_signature(dst_dir)
    if src_signature and src_signature == dst_signature:
        return False

    _safe_remove(dst_dir)
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dst_dir)
    return True


def _functional_lock_payload(payload: dict) -> dict:
    return {
        "source": payload.get("source", {}),
        "assets": payload.get("assets", {}),
    }


def _write_lock_if_changed(lock_path: Path, lock_payload: dict) -> bool:
    existing_payload = {}
    if lock_path.exists():
        try:
            existing_payload = json.loads(lock_path.read_text("utf-8"))
        except Exception:
            existing_payload = {}

    if _functional_lock_payload(existing_payload) == _functional_lock_payload(lock_payload):
        return False

    payload_to_write = {
        **lock_payload,
        "refreshed_at_utc": _now_iso_utc(),
    }
    lock_path.write_text(json.dumps(payload_to_write, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return True


def _extract_wps365_skill(skill_zip_bytes: bytes, dst_skill_dir: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="agentspace-skill-") as td:
        zip_path = Path(td) / "skill.zip"
        zip_path.write_bytes(skill_zip_bytes)
        unpack_dir = Path(td) / "unpack"
        unpack_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract_zip(zip_path, unpack_dir)

        # Prefer directory containing SKILL.md + requirements.txt.
        candidates = []
        for p in [unpack_dir] + [x for x in unpack_dir.iterdir() if x.is_dir()]:
            if (p / "SKILL.md").exists() and (p / "requirements.txt").exists():
                candidates.append(p)
        if not candidates:
            raise RuntimeError("failed to locate wps365 skill root in zip package")
        src_root = candidates[0]
        return _sync_directory_if_changed(src_root, dst_skill_dir)


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh OpenClaw Agentspace plugin/skill assets.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[3]),
        help="path to ksadk-python repository root",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    deploy_root = repo_root / "deploy" / "openclaw"
    lock_path = deploy_root / "agentspace-assets.lock.json"
    skill_dir = deploy_root / "preset-skills" / "wps365-skill"

    installer_bytes, installer_final_url = _read_url(INSTALLER_ENTRY_URL)
    installer_sha256 = _sha256_bytes(installer_bytes)
    installer_version, plugin_url, skills_url = _extract_installer_urls(installer_bytes)

    plugin_bytes, plugin_final_url = _read_url(plugin_url)
    plugin_sha256 = _sha256_bytes(plugin_bytes)

    skills_bytes, skills_final_url = _read_url(skills_url)
    skills_sha256 = _sha256_bytes(skills_bytes)

    skill_updated = _extract_wps365_skill(skills_bytes, skill_dir)

    lock_payload = {
        "source": {
            "installer_entry_url": INSTALLER_ENTRY_URL,
            "installer_resolved_url": installer_final_url,
            "installer_sha256": installer_sha256,
            "installer_version": installer_version,
        },
        "assets": {
            "agentspace_plugin": {
                "url": plugin_final_url,
                "sha256": plugin_sha256,
            },
            "wps365_skill": {
                "url": skills_final_url,
                "sha256": skills_sha256,
                "preset_path": "deploy/openclaw/preset-skills/wps365-skill",
            },
        },
    }
    lock_updated = _write_lock_if_changed(lock_path, lock_payload)

    print(f"[agentspace-refresh] installer: {installer_final_url}")
    print(f"[agentspace-refresh] installer version: {installer_version}")
    print(f"[agentspace-refresh] plugin: {plugin_final_url}")
    print(f"[agentspace-refresh] skills: {skills_final_url}")
    print(
        f"[agentspace-refresh] {'wrote' if lock_updated else 'kept'} lock: {lock_path}"
    )
    print(
        f"[agentspace-refresh] {'updated' if skill_updated else 'kept'} skill dir: {skill_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
