"""检查公开发布状态。

发布前使用 `--phase pre-publish`，确保 GitHub Pages 可访问且 PyPI 上还没有
当前版本；发布后使用 `--phase post-publish`，确保 PyPI 已能查询到当前版本。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _current_version() -> str:
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("pyproject.toml 中未找到 version")


def _open(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "ksadk-publication-check"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read()


def _expect_http_ok(name: str, url: str) -> None:
    status, _ = _open(url)
    if status != 200:
        raise RuntimeError(f"{name}: 期望 HTTP 200，实际 {status}: {url}")
    print(f"{name}: HTTP {status}")


def _pypi_project_version(project: str) -> str | None:
    url = f"https://pypi.org/pypi/{project}/json"
    try:
        status, body = _open(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if status != 200:
        raise RuntimeError(f"{project}: 期望 HTTP 200，实际 {status}: {url}")
    data = json.loads(body)
    return data.get("info", {}).get("version")


def _pypi_version_exists(project: str, version: str) -> bool:
    url = f"https://pypi.org/pypi/{project}/{version}/json"
    try:
        status, _ = _open(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    if status != 200:
        raise RuntimeError(f"{project}=={version}: 期望 HTTP 200 或 404，实际 {status}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("pre-publish", "post-publish"), required=True)
    parser.add_argument("--version", default=_current_version())
    parser.add_argument("--project", default="ksadk")
    parser.add_argument("--alias-project", default="agentengine-sdk-python")
    parser.add_argument("--docs-url", default="https://kingsoftcloud.github.io/ksadk-python/")
    args = parser.parse_args()

    _expect_http_ok("docs", args.docs_url)

    latest = _pypi_project_version(args.project)
    print(f"pypi:{args.project}: latest={latest}")

    exists = _pypi_version_exists(args.project, args.version)
    print(f"pypi:{args.project}=={args.version}: exists={exists}")

    alias_latest = _pypi_project_version(args.alias_project)
    print(f"pypi:{args.alias_project}: latest={alias_latest}")

    if args.phase == "pre-publish" and exists:
        raise RuntimeError(f"发布前检查失败：PyPI 已存在 {args.project}=={args.version}")
    if args.phase == "post-publish" and not exists:
        raise RuntimeError(f"发布后检查失败：PyPI 尚未存在 {args.project}=={args.version}")

    print(f"✅ publication {args.phase} check passed for {args.project}=={args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
