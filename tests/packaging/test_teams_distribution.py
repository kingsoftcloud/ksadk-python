"""Validate the installed Teams graph and generated Studio payload in release archives."""

from __future__ import annotations

import json
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "ksadk/plugins/providers/bundles"


@pytest.fixture(scope="module")
def archives():
    wheels = list((ROOT / "dist").glob("ksadk-*.whl"))
    sdists = list((ROOT / "dist").glob("ksadk-*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "Build exactly one wheel and sdist first"
    with zipfile.ZipFile(wheels[0]) as wheel, tarfile.open(sdists[0]) as sdist:
        prefix = sdists[0].name.removesuffix(".tar.gz") + "/"
        yield wheel, sdist, prefix


def _require_exact_file(archives, source: Path) -> None:
    wheel, sdist, prefix = archives
    relative = source.relative_to(ROOT).as_posix()
    expected = source.read_bytes()
    assert wheel.read(relative) == expected, relative
    member = sdist.extractfile(prefix + relative)
    assert member is not None, relative
    assert member.read() == expected, relative


def test_archives_include_the_complete_teams_graph_and_python_companion(archives):
    for package in sorted(PACKAGES.glob("dsh-teams-*")):
        manifest = json.loads((package / "package.json").read_text())
        assert manifest["version"] == "0.1.0"
        assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
        for target in manifest["exports"].values():
            assert isinstance(target, str)
            assert (package / target).is_file()
        for path in sorted(package.rglob("*")):
            if path.is_file():
                _require_exact_file(archives, path)
    assert len(list(PACKAGES.glob("dsh-teams-*"))) == 6
    for path in sorted((ROOT / "ksadk/plugins/teams").glob("*.py")):
        _require_exact_file(archives, path)
    for relative in (
        "ksadk/plugins/companions.py", "ksadk/plugins/companion_artifacts.py",
        "ksadk/plugins/dsh_teams.py", "ksadk/plugins/dsh_home.py",
        "ksadk/plugins/execution_host.py", "ksadk/studio/execution_host.py",
        "ksadk/studio/kernel_registry.py", "ksadk/studio/teams_installation.py",
        "ksadk/studio/workspace_plugins.py",
        "ksadk/plugins/providers/bundles/ksadk-dsh-companion-host/index.mjs",
        "ksadk/plugins/providers/bundles/ksadk-dsh-companion-host/client.mjs",
    ):
        _require_exact_file(archives, ROOT / relative)


def test_archives_byte_match_the_frozen_studio_static_tree(archives):
    wheel, sdist, prefix = archives
    root = ROOT / "ksadk/studio/static"
    files = sorted(path for path in root.rglob("*") if path.is_file())
    assert root / "index.html" in files
    assert any(path.suffix == ".js" for path in files)
    expected = {path.relative_to(ROOT).as_posix() for path in files}
    actual = {
        name for name in wheel.namelist()
        if name.startswith("ksadk/studio/static/") and not name.endswith("/")
    }
    assert actual == expected
    assert {
        member.name.removeprefix(prefix) for member in sdist.getmembers()
        if member.isfile() and member.name.startswith(prefix + "ksadk/studio/static/")
    } == expected
    for path in files:
        _require_exact_file(archives, path)
