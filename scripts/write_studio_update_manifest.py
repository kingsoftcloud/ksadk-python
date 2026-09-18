#!/usr/bin/env python3
"""Generate electron-updater channel metadata (latest-mac.yml / latest.yml).

We ship hand-rolled bundles (no electron-builder), so electron-builder's
publish step never writes these files. This script produces them from the
signed/unsigned artifact so electron-updater can discover updates from the
GitHub Release that release-studio-app.yml uploads them to.

The files[].url is a bare filename — the GitHub provider concatenates it with
the release asset URL. path mirrors the filename for legacy-client compat.
sha512 is the base64-encoded SHA-512 of the artifact (electron-updater format).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from pathlib import Path


def sha512_b64(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode("ascii")


def write_manifest(version: str, artifact: Path, release_date: str, out: Path, channel: str) -> None:
    # Strip a leading 'v' so the manifest version matches app.getVersion()
    # (package.json has no 'v' prefix). semver tolerates the prefix, but
    # internal electron-updater string compares can misfire on the mismatch.
    version = version.lstrip("v")
    filename = artifact.name
    digest = sha512_b64(artifact)
    # PyYAML is not guaranteed in CI; emit by hand. The structure matches
    # electron-builder's updateInfoBuilder output (files[] + legacy path/sha512).
    lines = [
        "version: " + version,
        "files:",
        f"  - url: {filename}",
        f"    sha512: {digest}",
        f"    size: {artifact.stat().st_size}",
        f"path: {filename}",
        f"sha512: {digest}",
        f"releaseDate: {release_date}",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", required=True, help="Release version (e.g. 0.8.4)")
    p.add_argument("--artifact", required=True, type=Path, help="Update artifact (.zip / .exe)")
    p.add_argument("--release-date", required=True, help="ISO-8601 release timestamp")
    p.add_argument("--channel", required=True, choices=["mac", "win"], help="Target platform channel")
    p.add_argument("--out", required=True, type=Path, help="Output manifest path")
    args = p.parse_args()

    if not args.artifact.is_file():
        print(f"ERROR: artifact not found: {args.artifact}", file=sys.stderr)
        return 1
    write_manifest(args.version, args.artifact, args.release_date, args.out, args.channel)
    print(f"wrote {args.out} for {args.artifact.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
