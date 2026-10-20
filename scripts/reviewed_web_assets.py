#!/usr/bin/env python3
"""Bind built-in Hosted UI, Studio and wheel assets to one reviewed Web package."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("ksadk/studio/react-ui/vendor/ksadk-web-source.json")
MARKER = "ui-source.json"


def tree_digest(root: Path, *, skip_marker: bool = False) -> str:
    digest = hashlib.sha256()
    files = sorted((p for p in root.rglob("*") if p.is_file()),
                   key=lambda p: p.relative_to(root).as_posix())
    if not files:
        raise ValueError(f"empty UI distribution: {root}")
    for path in files:
        name = path.relative_to(root).as_posix()
        if skip_marker and name == MARKER:
            continue
        if path.is_symlink():
            raise ValueError("UI distribution contains a symlink")
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode() + b"\n")
    return digest.hexdigest()


def manifest(root: Path) -> dict:
    return json.loads((root / MANIFEST).read_text())


def identity(value: dict) -> dict:
    return {key: value[key] for key in (
        "source_commit", "package_name", "package_version", "artifact_sha256",
        "library_tree_sha256", "hosted_tree_sha256", "registry_release",
    )}


def verify_package(root: Path, destination: Path, artifact: Path | None = None) -> dict:
    value = manifest(root)
    data = (artifact or root / MANIFEST.parent / value["artifact"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != value["artifact_sha256"]:
        raise ValueError("Web archive differs from reviewed source; no fallback is allowed")
    # Validate the complete archive before writing; never follow links or use extractall.
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        entries = archive.getmembers()
        seen = set()
        if sum(item.size for item in entries) > 128 * 1024 * 1024:
            raise ValueError("Web archive exceeds the unpacked size limit")
        for item in entries:
            path = PurePosixPath(item.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != "package" or "\\" in item.name
                    or not (item.isfile() or item.isdir()) or item.name in seen):
                raise ValueError(f"unsafe Web archive member: {item.name}")
            seen.add(item.name)
        for item in entries:
            target = destination.joinpath(*PurePosixPath(item.name).parts)
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(item).read())
    package = destination / "package"
    metadata = json.loads((package / "package.json").read_text())
    source = json.loads((package / "SOURCE_CANDIDATE.json").read_text())
    if (metadata.get("name") != value["package_name"]
            or metadata.get("version") != value["package_version"]
            or source.get("source_commit") != value["source_commit"]):
        raise ValueError("Web package metadata does not match reviewed source")
    for directory, key in (("dist-lib", "library_tree_sha256"),
                           ("dist-ksadk", "hosted_tree_sha256")):
        if tree_digest(package / directory) != value[key]:
            raise ValueError(f"Web {directory} does not match reviewed source")
    return value


def stamp(root: Path, directory: Path, role: str, value: dict) -> None:
    if not (directory / "index.html").is_file():
        raise ValueError(f"{role} entrypoint is missing")
    payload = {**identity(value), "role": role,
               "static_tree_sha256": tree_digest(directory, skip_marker=True)}
    (directory / MARKER).write_text(json.dumps(payload, indent=2) + "\n")


def verify_static(directory: Path, role: str, value: dict) -> dict:
    if not (directory / MARKER).is_file():
        raise ValueError(
            f"{role} static assets are missing; run make build-frontend before packaging"
        )
    marker = json.loads((directory / MARKER).read_text())
    if any(marker.get(key) != expected for key, expected in identity(value).items()):
        raise ValueError(f"{role} source differs from the reviewed Web package")
    if (marker.get("role") != role or marker.get("static_tree_sha256")
            != tree_digest(directory, skip_marker=True)):
        raise ValueError(f"{role} static assets changed after the build")
    if role == "hosted" and marker["static_tree_sha256"] != value["hosted_tree_sha256"]:
        raise ValueError("Hosted UI contains another Web distribution")
    return marker


def sync(root: Path, artifact: Path | None = None) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary)
        value = verify_package(root, staging, artifact)
        destination = root / "ksadk/server/static"
        # The package and both trees were verified before replacing generated assets.
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staging / "package/dist-ksadk", destination)
        stamp(root, destination, "hosted", value)


def verify(root: Path, wheel: Path | None = None) -> dict:
    value = manifest(root)
    if wheel:
        with tempfile.TemporaryDirectory() as temporary, zipfile.ZipFile(wheel) as archive:
            staging = Path(temporary)
            members = archive.infolist()
            if sum(item.file_size for item in members) > 128 * 1024 * 1024:
                raise ValueError("wheel exceeds the unpacked size limit")
            for item in members:
                name = item.filename
                if name.startswith(("ksadk/server/static/", "ksadk/studio/static/")):
                    path = PurePosixPath(name)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("unsafe wheel member")
                    if not name.endswith("/"):
                        target = staging.joinpath(*path.parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(name))
            return verify_trees(staging, value)
    return verify_trees(root, value)


def verify_trees(root: Path, value: dict) -> dict:
    return {role: verify_static(root / path, role, value) for role, path in (
        ("hosted", "ksadk/server/static"), ("studio", "ksadk/studio/static"),
    )}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=[
        "identity", "check-archive", "sync", "stamp-studio", "verify",
    ])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--expected-version")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--field")
    args = parser.parse_args()
    try:
        value = manifest(args.root)
        if args.expected_version and args.expected_version != value["package_version"]:
            raise ValueError("requested Web version differs from reviewed source manifest")
        if args.action == "identity":
            print(value[args.field] if args.field else json.dumps(identity(value)))
        elif args.action == "check-archive":
            with tempfile.TemporaryDirectory() as temporary:
                verify_package(args.root, Path(temporary), args.artifact)
            print(json.dumps(identity(value)))
        elif args.action == "sync":
            sync(args.root, args.artifact)
        elif args.action == "stamp-studio":
            stamp(args.root, args.root / "ksadk/studio/static", "studio", value)
        else:
            print(json.dumps(verify(args.root, args.wheel), indent=2))
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
