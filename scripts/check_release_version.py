#!/usr/bin/env python3
"""发版版本号门禁:防止降版或重复发版。

读取 ksadk/version.py 当前版本,对比 PyPI 已发版本,如果当前版本 <= PyPI
已发版本则失败。用于 make public-preflight,在发布前拦截降版/重复发版。

用法:
    uv run python scripts/check_release_version.py
    uv run python scripts/check_release_version.py --project ksadk
    uv run python scripts/check_release_version.py --skip-alias

退出码:
    0  当前版本高于 PyPI 已发版本(或 PyPI 不可达且 --allow-offline)
    1  当前版本 <= PyPI 已发版本(降版或重复发版),阻止发布
    2  PyPI 不可达且未传 --allow-offline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PROJECT = "ksadk"
DEFAULT_ALIAS_PROJECT = "agentengine-sdk-python"
PYPI_JSON_URL = "https://pypi.org/pypi/{project}/json"
VERSION_PY = "ksadk/version.py"

# SemVer-ish: 主.次.修(+ 可选 pre/build)。比较时把各段当 int。
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].+)?$")


def parse_version(v: str) -> tuple[int, int, int]:
    """解析 x.y.z 为 (major, minor, patch) 整数元组。非 SemVer 返回 (0,0,0)。"""
    m = _VERSION_RE.match(v.strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def read_local_version(repo_root: Path) -> str:
    """从 ksadk/version.py 读 VERSION 常量。"""
    version_py = repo_root / VERSION_PY
    if not version_py.exists():
        raise SystemExit(f"❌ 找不到 {version_py}")
    text = version_py.read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not m:
        raise SystemExit(f"❌ {version_py} 未找到 VERSION 常量")
    return m.group(1)


def fetch_pypi_version(project: str, timeout: float = 20.0) -> str | None:
    """查 PyPI JSON API,返回已发版本号;不可达返回 None。"""
    url = PYPI_JSON_URL.format(project=project)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"⚠️  PyPI {project} 查询失败: {e}", file=sys.stderr)
        return None
    info = data.get("info") or {}
    return info.get("version")


def compare(local: str, published: str) -> int:
    """返回 -1/0/1:local < published / == / >。"""
    lv, pv = parse_version(local), parse_version(published)
    if lv < pv:
        return -1
    if lv > pv:
        return 1
    # 数字段相等时,纯数字版 > 带 pre/build 的版(0.6.7 > 0.6.7rc1)
    local_clean = "-" not in local and "+" not in local
    pub_clean = "-" not in published and "+" not in published
    if local_clean and not pub_clean:
        return 1
    if not local_clean and pub_clean:
        return -1
    return 0


def check_project(project: str, local_version: str, allow_offline: bool) -> int:
    """检查单个 project。返回退出码。"""
    published = fetch_pypi_version(project)
    if published is None:
        if allow_offline:
            print(f"⚠️  {project}: PyPI 不可达,跳过(--allow-offline),本地版本 {local_version}")
            return 0
        print(f"❌ {project}: PyPI 不可达,无法确认是否降版;如确认网络隔离可传 --allow-offline 跳过", file=sys.stderr)
        return 2

    cmp = compare(local_version, published)
    if cmp < 0:
        print(f"❌ {project}: 本地版本 {local_version} < PyPI 已发版本 {published},降版被禁止", file=sys.stderr)
        return 1
    if cmp == 0:
        print(f"❌ {project}: 本地版本 {local_version} == PyPI 已发版本 {published},重复发版被禁止(bump 版本号后再发)", file=sys.stderr)
        return 1
    print(f"✅ {project}: 本地版本 {local_version} > PyPI 已发版本 {published}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="发版版本号门禁")
    ap.add_argument("--project", default=DEFAULT_PROJECT, help=f"主 PyPI 项目名(默认 {DEFAULT_PROJECT})")
    ap.add_argument("--alias-project", default=DEFAULT_ALIAS_PROJECT, help=f"别名包项目名(默认 {DEFAULT_ALIAS_PROJECT})")
    ap.add_argument("--skip-alias", action="store_true", help="跳过别名包检查")
    ap.add_argument("--allow-offline", action="store_true", help="PyPI 不可达时跳过而非失败(用于网络隔离环境)")
    ap.add_argument("--repo-root", default=".", help="仓库根目录(默认当前目录)")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    local_version = read_local_version(repo_root)
    print(f"==> 版本门禁:本地 {args.project}={local_version}")

    rc = check_project(args.project, local_version, args.allow_offline)
    if rc != 0:
        return rc

    if not args.skip_alias:
        rc = check_project(args.alias_project, local_version, args.allow_offline)
        if rc != 0:
            return rc

    print(f"✅ 版本门禁通过:本地版本 {local_version} 高于所有已发版本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
