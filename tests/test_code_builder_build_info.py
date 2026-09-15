"""BUILD-INFO 来源清单测试:zip 里 vendored ksadk 的来源/commit/指纹可追溯。"""

import importlib.metadata as importlib_metadata
import json
import zipfile
from pathlib import Path

import pytest

from ksadk.builders.code_builder import (
    BUILD_INFO_ARCNAME,
    BUILD_INFO_SCHEMA,
    _bundled_content_fingerprint,
    _is_release_like_source,
    _package_provenance,
    build_bundled_source_manifest,
)


def _touch(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_bundled_content_fingerprint_is_deterministic(tmp_path):
    pkg = tmp_path / "pkg"
    files = [
        ("b.py", _touch(pkg / "b.py", b"b1")),
        ("a.py", _touch(pkg / "a.py", b"a1")),
    ]
    first = _bundled_content_fingerprint(files)
    # 顺序变化不影响指纹
    assert _bundled_content_fingerprint(list(reversed(files))) == first
    # 内容变化指纹变化
    files[0][1].write_bytes(b"b2")
    assert _bundled_content_fingerprint(files) != first


def test_build_bundled_source_manifest_shape(tmp_path):
    pkg = tmp_path / "ksadk"
    files = [
        ("__init__.py", _touch(pkg / "__init__.py", b"")),
        ("a2a/routes.py", _touch(pkg / "a2a" / "routes.py", b"x")),
    ]
    manifest = build_bundled_source_manifest({"ksadk": pkg}, [("ksadk", r, p) for r, p in files])

    assert manifest["schema"] == BUILD_INFO_SCHEMA
    assert "built_at" in manifest
    info = manifest["packages"]["ksadk"]
    assert info["file_count"] == 2
    assert info["source_dir"] == str(pkg)
    assert len(info["content_fingerprint_sha256"]) == 64
    # 非 git / 无 dist 时 source_type 兜底为 unknown(或 installed-dist,取决于环境)
    assert info["source_type"] in {"unknown", "installed-dist", "editable-install", "local-path"}


def test_package_provenance_reads_dist_info(monkeypatch, tmp_path):
    class _FakeDist:
        version = "0.8.1"

        def read_text(self, name):
            if name == "INSTALLER":
                return "pip\n"
            if name == "direct_url.json":
                return None
            return None

    monkeypatch.setattr(importlib_metadata, "distribution", lambda name: _FakeDist())

    info = _package_provenance("ksadk", tmp_path)
    assert info["dist_version"] == "0.8.1"
    assert info["installer"] == "pip"
    # 无 direct_url 时视为 index 安装的正式 dist
    assert info["source_type"] == "installed-dist"
    assert _is_release_like_source(info)


def test_package_provenance_detects_editable(monkeypatch, tmp_path):
    class _FakeDist:
        version = "0.8.1"

        def read_text(self, name):
            if name == "INSTALLER":
                return "pip\n"
            if name == "direct_url.json":
                return json.dumps(
                    {"url": "file:///Users/dev/ksadk-python", "dir_info": {"editable": True}}
                )
            return None

    monkeypatch.setattr(importlib_metadata, "distribution", lambda name: _FakeDist())

    info = _package_provenance("ksadk", tmp_path)
    assert info["source_type"] == "editable-install"
    assert not _is_release_like_source(info)


def test_package_provenance_without_dist(monkeypatch, tmp_path):
    def _raise(name):
        raise importlib_metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib_metadata, "distribution", _raise)
    info = _package_provenance("ksadk", tmp_path)
    assert info["dist_version"] is None
    assert info["source_type"] == "unknown"
    assert not _is_release_like_source(info)


def test_zip_contains_build_info(tmp_path, monkeypatch):
    """端到端:打包出的 zip 必须带 ksadk/BUILD-INFO.json 且指纹可复算。"""

    from ksadk.builders.code_builder import CodeBuilder

    builder = CodeBuilder.__new__(CodeBuilder)
    # 最小化构造:只需要 _bundled_source_package_roots / _iter_bundled_source_files /
    # _should_skip_ksadk_relative_path 可用
    pkg = tmp_path / "ksadk"
    common = tmp_path / "ksadk_runtime_common"
    _touch(pkg / "__init__.py", b"")
    routes = _touch(pkg / "a2a" / "routes.py", b"routes-content")
    _touch(common / "__init__.py", b"")

    monkeypatch.setattr(
        CodeBuilder,
        "_bundled_source_package_roots",
        lambda self: {"ksadk": pkg, "ksadk_runtime_common": common},
    )

    bundled = list(builder._iter_bundled_source_files())
    manifest = build_bundled_source_manifest(builder._bundled_source_package_roots(), bundled)

    zip_path = tmp_path / "code.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for package_name, relative, file_path in bundled:
            zf.write(file_path, f"{package_name}/{relative}")
        zf.writestr(
            BUILD_INFO_ARCNAME,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert BUILD_INFO_ARCNAME in names
        payload = json.loads(zf.read(BUILD_INFO_ARCNAME))

    assert payload["schema"] == BUILD_INFO_SCHEMA
    ksadk_info = payload["packages"]["ksadk"]
    assert ksadk_info["file_count"] == 2
    # 用同一函数对 zip 内文件复算,验证可追溯
    rel_files = [(r, p) for n, r, p in bundled if n == "ksadk"]
    assert ksadk_info["content_fingerprint_sha256"] == _bundled_content_fingerprint(rel_files)
    assert payload["packages"]["ksadk_runtime_common"]["file_count"] == 1
    assert "ksadk/a2a/routes.py" in names
