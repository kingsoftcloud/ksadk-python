#!/usr/bin/env python3
"""Audit built sdist and wheel artifacts for public release readiness."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Sequence


REQUIRED_RUNTIME_COMMON_FILES = (
    "ksadk_runtime_common/__init__.py",
    "ksadk_runtime_common/workspace_files/__init__.py",
    "ksadk_runtime_common/workspace_files/router.py",
    "ksadk_runtime_common/memory_backend/__init__.py",
    "ksadk_runtime_common/memory_backend/render.py",
    "ksadk_runtime_common/memory_backend/providers/mem0.py",
    "ksadk_runtime_common/memory_backend/providers/lancedb.py",
    "ksadk_runtime_common/schemas/memory_backend_manifest.schema.json",
)


def run_audit(target: str, names: Sequence[str]) -> None:
    subprocess.run(
        [sys.executable, "scripts/open_source_audit.py", "--target", target, "--file-list", "-"],
        input="\n".join(names) + "\n",
        text=True,
        check=True,
    )


def run_extracted_content_audit(target: str, root: Path) -> None:
    subprocess.run(
        [sys.executable, "scripts/open_source_audit.py", "--target", target, "--root", str(root)],
        text=True,
        check=True,
    )


def audit_sdist(path: Path) -> None:
    with tarfile.open(path) as archive:
        names = archive.getnames()
        with tempfile.TemporaryDirectory(prefix="ksadk-sdist-audit-") as tmpdir:
            archive.extractall(tmpdir, filter="data")
            print(f"auditing sdist content: {path}")
            run_extracted_content_audit("sdist", Path(tmpdir))
    print(f"auditing sdist: {path} ({len(names)} files)")
    run_audit("sdist", names)
    normalized = [_strip_sdist_root(name) for name in names]
    audit_runtime_common_files(path, normalized)


def audit_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        with tempfile.TemporaryDirectory(prefix="ksadk-wheel-audit-") as tmpdir:
            archive.extractall(tmpdir)
            print(f"auditing wheel content: {path}")
            run_extracted_content_audit("wheel", Path(tmpdir))
    print(f"auditing wheel: {path} ({len(names)} files)")
    run_audit("wheel", names)
    audit_runtime_common_files(path, names)


def _strip_sdist_root(name: str) -> str:
    parts = name.split("/", 1)
    return parts[1] if len(parts) == 2 else name


def audit_runtime_common_files(path: Path, names: Sequence[str]) -> None:
    name_set = set(names)
    missing = [name for name in REQUIRED_RUNTIME_COMMON_FILES if name not in name_set]
    if missing:
        joined = "\n  - ".join(missing)
        raise RuntimeError(f"{path} is missing required runtime common files:\n  - {joined}")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dist_dir",
        nargs="?",
        default="dist",
        type=Path,
        help="directory containing built .tar.gz and .whl artifacts",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    dist_dir = args.dist_dir
    if not dist_dir.is_dir():
        print(f"dist directory not found: {dist_dir}", file=sys.stderr)
        return 1

    sdists = sorted(dist_dir.glob("*.tar.gz"))
    wheels = sorted(dist_dir.glob("*.whl"))
    if not sdists:
        print(f"no sdist artifacts found in {dist_dir}", file=sys.stderr)
        return 1
    if not wheels:
        print(f"no wheel artifacts found in {dist_dir}", file=sys.stderr)
        return 1

    for path in sdists:
        audit_sdist(path)
    for path in wheels:
        audit_wheel(path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
