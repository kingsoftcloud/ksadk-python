"""Opt-in npm E2E for the managed DSH plugin developer toolchain."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ksadk.plugins.dsh_toolchain import (
    DSH_PACKAGE_SPEC,
    DSH_VERSION,
    DshPluginDeveloper,
    DshToolchainManager,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_DSH_TOOLCHAIN_E2E") != "1",
    reason="set KSADK_DSH_TOOLCHAIN_E2E=1 to install the pinned public npm toolchain",
)


def test_public_npm_toolchain_validates_generated_tgz_and_official_plugin(
    tmp_path: Path,
) -> None:
    manager = DshToolchainManager(base_dir=tmp_path / "toolchains")
    state = manager.install()
    assert state.usable is True
    assert state.actual_version == DSH_VERSION

    developer = DshPluginDeveloper(toolchain=manager)
    source = tmp_path / "example-plugin"
    created = developer.create(source, package_name="dsh-agentengine-example")
    assert created.package_name == "dsh-agentengine-example"

    source_validation = developer.validate(source)
    assert source_validation.host_version == DSH_VERSION
    packed = developer.pack(source)
    archive_validation = developer.validate(Path(packed.artifact))
    assert archive_validation.package_name == created.package_name

    official = developer.validate("@deepseek-ai/dsh-subagent-codex@0.1.1-rc.2")
    assert official.package_name == "@deepseek-ai/dsh-subagent-codex"
    assert official.package_version == DSH_VERSION
    assert DSH_PACKAGE_SPEC == "@deepseek-ai/dsh@0.1.1-rc.2"
