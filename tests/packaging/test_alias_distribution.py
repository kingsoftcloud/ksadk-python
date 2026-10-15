"""Compatibility distribution extras must not install a second SDK owner."""

import importlib.util
from pathlib import Path
import tomllib

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[2]


def test_alias_preserves_sdk_dependencies_and_owns_its_extra_expansion(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "build_alias_distribution", ROOT / "scripts/build_alias_distribution.py",
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    source = ROOT / "pyproject.toml"
    target = tmp_path / "pyproject.toml"
    target.write_bytes(source.read_bytes())
    before = tomllib.loads(target.read_text())["project"]
    builder._rewrite_pyproject(target, alias_project="agentengine-sdk-python", description="SDK alias")
    after = tomllib.loads(target.read_text())["project"]

    assert after["name"] == "agentengine-sdk-python"
    assert after["version"] == before["version"]
    assert after["dependencies"] == before["dependencies"]
    assert after["optional-dependencies"].keys() == before["optional-dependencies"].keys()
    for group, original in before["optional-dependencies"].items():
        updated = after["optional-dependencies"][group]
        assert len(updated) == len(original)
        for raw_before, raw_after in zip(original, updated, strict=True):
            previous, current = Requirement(raw_before), Requirement(raw_after)
            if previous.name == "ksadk":
                assert current.name == after["name"]
                assert current.extras == previous.extras
                assert current.specifier == previous.specifier
                assert current.marker == previous.marker
                assert current.extras <= after["optional-dependencies"].keys()
            else:
                assert current == previous
    assert any(Requirement(raw).name == after["name"]
               for raw in after["optional-dependencies"]["all"])
