#!/usr/bin/env python3
"""Verify the Web static payload embedded in a KsADK release artifact.

The Python package intentionally consumes the published ``dist-ksadk`` payload
instead of rebuilding a checkout of ksadk-web. This checker makes that boundary
explicit: the source package version must match the requested pin and every
static file in the wheel must byte-match the payload extracted from the same
tarball.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

STATIC_PREFIX = "ksadk/server/static/"


def _tree_digest(root: Path) -> tuple[str, tuple[str, ...]]:
    if not root.is_dir():
        raise ValueError(f"static directory does not exist: {root}")

    digest = hashlib.sha256()
    paths: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        paths.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    if not paths:
        raise ValueError(f"static directory is empty: {root}")
    return digest.hexdigest(), tuple(paths)


def _wheel_digest(wheel: Path) -> tuple[str, tuple[str, ...]]:
    if not wheel.is_file():
        raise ValueError(f"wheel does not exist: {wheel}")

    digest = hashlib.sha256()
    paths: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        for name in sorted(
            entry for entry in archive.namelist() if entry.startswith(STATIC_PREFIX)
        ):
            if name.endswith("/"):
                continue
            relative = name.removeprefix(STATIC_PREFIX)
            paths.append(relative)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(archive.read(name)).digest())
            digest.update(b"\n")
    if not paths:
        raise ValueError(f"wheel does not contain {STATIC_PREFIX}: {wheel}")
    return digest.hexdigest(), tuple(paths)


def _require_package_version(package_root: Path, expected_version: str) -> None:
    package_json = package_root / "package.json"
    if not package_json.is_file():
        raise ValueError(f"Web package metadata missing: {package_json}")
    metadata = json.loads(package_json.read_text(encoding="utf-8"))
    actual_version = str(metadata.get("version") or "")
    if actual_version != expected_version:
        raise ValueError(
            "Web package version mismatch: "
            f"expected {expected_version}, got {actual_version or '<missing>'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True, type=Path)
    parser.add_argument("--actual", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()

    if bool(args.actual) == bool(args.wheel):
        parser.error("provide exactly one of --actual or --wheel")

    try:
        expected_digest, expected_paths = _tree_digest(args.expected)
        if args.package_root:
            _require_package_version(args.package_root, args.expected_version)
        if args.actual:
            actual_digest, actual_paths = _tree_digest(args.actual)
            subject = str(args.actual)
        else:
            actual_digest, actual_paths = _wheel_digest(args.wheel)
            subject = str(args.wheel)
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if expected_paths != actual_paths or expected_digest != actual_digest:
        print(
            "ERROR: KsADK Web static payload does not match the pinned dist-ksadk",
            file=sys.stderr,
        )
        print(f"  expected: {args.expected} ({expected_digest})", file=sys.stderr)
        print(f"  actual:   {subject} ({actual_digest})", file=sys.stderr)
        return 1

    print(
        f"Verified KsADK Web {args.expected_version} static payload: "
        f"{len(expected_paths)} files, sha256={expected_digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
