#!/usr/bin/env python3
"""Use a reviewed Web package by default, or an explicit local npm tarball."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("ksadk/studio/react-ui/vendor/ksadk-web-source.json")
MARKER = "ui-source.json"
PACKAGE_NAME = "@kingsoftcloud/ksadk-web"


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
    result = {key: value[key] for key in (
        "source_commit", "package_name", "package_version", "artifact_sha256",
        "library_tree_sha256", "hosted_tree_sha256", "registry_release",
    )}
    if value.get("source_type") == "local":
        result["source_type"] = "local"
    return result


def verify_package(root: Path, destination: Path, artifact: Path | None = None) -> dict:
    # An explicit local archive selects a development build, not the pinned release.
    value = manifest(root) if artifact is None else None
    if value is not None and value.get("registry_release") is True:
        expected_url = (
            "https://registry.npmjs.org/@kingsoftcloud/ksadk-web/-/"
            f"ksadk-web-{value['package_version']}.tgz"
        )
        if value.get("artifact_url") != expected_url:
            raise ValueError("Released Web archive must use its pinned public npm URL")
        cache = root / ".cache/ksadk-web" / value["artifact_sha256"]
        downloaded = not cache.is_file()
        if not downloaded:
            data = cache.read_bytes()
        else:
            with urlopen(expected_url, timeout=60) as response:
                data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("Web archive exceeds the download size limit")
        if hashlib.sha256(data).hexdigest() != value["artifact_sha256"]:
            raise ValueError("Web archive differs from reviewed source; no fallback is allowed")
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        if integrity != value.get("artifact_integrity"):
            raise ValueError("Released Web archive differs from its npm integrity")
        if downloaded:
            # Publish verified downloads atomically for concurrent consumers.
            cache.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=cache.parent) as temporary:
                staged = Path(temporary) / "archive.tgz"
                staged.write_bytes(data)
                os.replace(staged, cache)
    else:
        source_path = (
            artifact if artifact is not None else root / MANIFEST.parent / value["artifact"]
        )
        data = source_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if value is not None and digest != value["artifact_sha256"]:
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
    expected_name = value["package_name"] if value is not None else PACKAGE_NAME
    if (not isinstance(metadata, dict) or metadata.get("name") != expected_name
            or not isinstance(metadata.get("version"), str) or not metadata["version"].strip()):
        raise ValueError("Web archive must contain a named, versioned KsADK Web npm package")
    if not (package / "dist-ksadk/index.html").is_file():
        raise ValueError("Web archive is missing dist-ksadk/index.html")
    trees = {key: tree_digest(package / directory) for directory, key in (
        ("dist-lib", "library_tree_sha256"), ("dist-ksadk", "hosted_tree_sha256"),
    )}
    if value is None:
        # Ordinary npm packs need no private SOURCE_CANDIDATE.json. Any commit
        # recorded by the package is a declaration, not verified release identity.
        source = {}
        for name in ("SOURCE_CANDIDATE.json", "RELEASE_PROVENANCE.json"):
            if (package / name).is_file():
                declared = json.loads((package / name).read_text())
                if isinstance(declared, dict):
                    source = declared
                break
        return {"source_commit": source.get("source_commit"),
                "package_name": metadata["name"], "package_version": metadata["version"],
                "artifact_sha256": digest, **trees, "registry_release": False,
                "source_type": "local"}
    source_file = (
        "RELEASE_PROVENANCE.json" if value.get("registry_release") else "SOURCE_CANDIDATE.json"
    )
    source = json.loads((package / source_file).read_text())
    if value.get("registry_release") and (
        source.get("package") != value["package_name"]
        or source.get("version") != value["package_version"]
    ):
        raise ValueError("Released Web provenance does not match the pinned npm package")
    if (metadata.get("name") != value["package_name"]
            or metadata.get("version") != value["package_version"]
            or source.get("source_commit") != value["source_commit"]):
        raise ValueError("Web package metadata does not match reviewed source")
    for directory, key in (("dist-lib", "library_tree_sha256"),
                           ("dist-ksadk", "hosted_tree_sha256")):
        if trees[key] != value[key]:
            raise ValueError(f"Web {directory} does not match reviewed source")
    return value


def selected_source(root: Path, artifact: Path | None = None) -> dict:
    if artifact is None:
        return manifest(root)
    with tempfile.TemporaryDirectory() as temporary:
        return verify_package(root, Path(temporary), artifact)


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
        # Validate the complete package before replacing generated assets.
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staging / "package/dist-ksadk", destination)
        stamp(root, destination, "hosted", value)


def verify(root: Path, wheel: Path | None = None, artifact: Path | None = None) -> dict:
    value = selected_source(root, artifact)
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
    parser.add_argument("--artifact", type=Path,
                        default=os.environ.get("KSADK_WEB_TARBALL") or None,
                        help="Explicit local npm tarball; skips pinned release identity checks")
    parser.add_argument("--expected-version")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--field")
    args = parser.parse_args()
    try:
        value = selected_source(args.root, args.artifact)
        if (args.artifact is None and args.expected_version
                and args.expected_version != value["package_version"]):
            raise ValueError("requested Web version differs from reviewed source manifest")
        if args.action == "identity":
            print(value[args.field] if args.field else json.dumps(identity(value)))
        elif args.action == "check-archive":
            with tempfile.TemporaryDirectory() as temporary:
                value = verify_package(args.root, Path(temporary), args.artifact)
            print(json.dumps(identity(value)))
        elif args.action == "sync":
            sync(args.root, args.artifact)
        elif args.action == "stamp-studio":
            stamp(args.root, args.root / "ksadk/studio/static", "studio", value)
        else:
            print(json.dumps(verify(args.root, args.wheel, args.artifact), indent=2))
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
