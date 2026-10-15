"""Default release assembly must execute Native without optional frameworks."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement

tomllib = importlib.import_module("tomllib" if sys.version_info >= (3, 11) else "tomli")

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("durable", [None, False, True])
def test_cold_default_studio_build_and_tools_without_framework_imports(streaming, durable):
    command = [sys.executable, str(ROOT / "scripts/verify_native_studio_install.py")]
    if streaming:
        command.append("--streaming")
    if durable is True:
        command.append("--durable")
    elif durable is False:
        command.append("--memory")
    environment = dict(os.environ, PYTHONPATH=str(ROOT))
    completed = subprocess.run(command, cwd=ROOT, env=environment, text=True,
                               capture_output=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"engine": "native"' in completed.stdout


def test_base_dependency_contract_keeps_managed_factory_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    names = {Requirement(raw).name for raw in project["dependencies"]}
    assert "langgraph" in names
    assert "langgraph-checkpoint-sqlite" in names
    assert not names & {"langchain", "langchain-core"}
    for name in ("langgraph", "langchain", "deepagents"):
        assert project["optional-dependencies"][name]


@pytest.mark.parametrize("configuration,default,enabled", [
    ({}, True, True),
    ({"native_harness_default": False}, False, False),
    ({"native_harness_agent_ids": ()}, False, False),
    ({"native_harness_agent_ids": ("one",)}, False, True),
    ({"native_harness_default": True, "native_harness_agent_ids": ("one",)}, True, True),
    ({"harness_reasoner": object()}, False, False),
])
@pytest.mark.asyncio
async def test_default_and_explicit_compatibility_selection(
    tmp_path, monkeypatch, configuration, default, enabled,
):
    from ksadk.studio.service import StudioService
    from tests.studio.test_dsh_provider_registration import _managed_profile

    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.delenv("KSADK_STUDIO_NATIVE_DEFAULT", raising=False)
    monkeypatch.delenv("KSADK_STUDIO_NATIVE_AGENT_IDS", raising=False)
    monkeypatch.delenv("KSADK_STUDIO_NATIVE_DURABLE", raising=False)
    studio = StudioService(tmp_path / "workspace", **configuration)
    try:
        assert studio._native_harness_default is default
        assert studio._native_harness_durable is default
        assert (studio._native_product_composer is not None) is enabled
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_native_default_environment_and_explicit_override(tmp_path, monkeypatch):
    from ksadk.studio.service import StudioService
    from tests.studio.test_dsh_provider_registration import _managed_profile

    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("KSADK_STUDIO_NATIVE_DEFAULT", "1")
    monkeypatch.setenv("KSADK_STUDIO_NATIVE_AGENT_IDS", "selected")
    studio = StudioService(tmp_path / "default")
    try:
        assert studio._native_harness_default is True
        compatibility = StudioService(tmp_path / "compatibility", native_harness_default=False,
                                      native_harness_agent_ids=())
        try:
            assert compatibility._native_product_composer is None
        finally:
            await compatibility.aclose()
    finally:
        await studio.aclose()


@pytest.mark.parametrize("value", ["maybe", "", "true"])
def test_invalid_native_default_fails_before_opening_workspace(tmp_path, monkeypatch, value):
    from ksadk.studio.service import StudioService

    monkeypatch.setenv("KSADK_STUDIO_NATIVE_DEFAULT", value)
    with pytest.raises(ValueError, match="KSADK_STUDIO_NATIVE_DEFAULT"):
        StudioService(tmp_path / "workspace")
    assert not (tmp_path / "workspace").exists()
