"""Collect immutable source and installed-package provenance."""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import json
import subprocess
from pathlib import Path
from typing import Any, Optional


def _git_provenance(start: Path) -> Optional[dict]:
    """返回 start 所在 git 仓库的提交信息;非 git 目录返回 None。"""

    def _git(*args: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(start), *args],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    toplevel = _git("rev-parse", "--show-toplevel")
    if not toplevel:
        return None
    commit = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    status = _git("status", "--porcelain")
    return {
        "repo_root": toplevel,
        "commit": commit,
        "branch": branch or None,
        "dirty": bool(status),
    }


def _package_provenance(package_name: str, package_root: Path, *, git_reader: Any) -> dict:
    """采集被 vendored 包的安装来源:dist 元信息 / 本地路径 / git 提交。"""

    info: dict[str, Any] = {
        "source_dir": str(package_root),
        "dist_version": None,
        "installer": None,
        "direct_url": None,
        "source_type": "unknown",
        "git": None,
    }
    try:
        dist = importlib_metadata.distribution(package_name)
    except Exception:
        dist = None
    if dist is not None:
        info["dist_version"] = dist.version
        installer = (dist.read_text("INSTALLER") or "").strip()
        info["installer"] = installer or None
        raw_direct_url = dist.read_text("direct_url.json")
        if raw_direct_url:
            try:
                direct_url = json.loads(raw_direct_url)
            except ValueError:
                direct_url = None
            if isinstance(direct_url, dict):
                info["direct_url"] = direct_url.get("url")
                url = str(direct_url.get("url") or "")
                dir_info = direct_url.get("dir_info") or {}
                archive_info = direct_url.get("archive_info") or {}
                if url.startswith("file://"):
                    if dir_info.get("editable"):
                        info["source_type"] = "editable-install"
                    elif url.endswith(".whl"):
                        info["source_type"] = "local-wheel"
                    else:
                        info["source_type"] = "local-path"
                elif archive_info:
                    info["source_type"] = "remote-dist"
    if info["direct_url"] is None and info["dist_version"] is not None:
        # pip 从 index 安装的常规 dist 通常不写 direct_url.json
        info["source_type"] = "installed-dist"
    if info["source_type"] in {"editable-install", "local-path", "unknown"}:
        info["git"] = git_reader(package_root)
    return info
