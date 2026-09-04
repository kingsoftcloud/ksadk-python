from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge, validate_dsh_registry_source
from ksadk.studio.api_plugin_routes import DshPluginInstallRequest


@pytest.mark.parametrize(
    "source",
    [
        "@deepseek-ai/dsh-tool-web@1.2.3",
        "tool-package@0.1.0-rc.1",
    ],
)
def test_exact_registry_sources_are_accepted(source: str) -> None:
    assert validate_dsh_registry_source(source) == source
    assert DshPluginInstallRequest(source=source).source == source


@pytest.mark.parametrize(
    "source",
    [
        "@deepseek-ai/dsh-tool-web",
        "@deepseek-ai/dsh-tool-web@latest",
        "@deepseek-ai/dsh-tool-web@^1.0.0",
        "https://github.com/example/plugin.git",
        "git+ssh://git@example.com/plugin.git",
        "github:example/plugin#main",
    ],
)
def test_mutable_or_git_sources_are_rejected(source: str) -> None:
    with pytest.raises(ValueError, match="exact-semver|Git URLs"):
        validate_dsh_registry_source(source)
    with pytest.raises(ValidationError, match="exact-semver|Git URLs"):
        DshPluginInstallRequest(source=source)


def test_local_source_is_cli_only_and_must_exist(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir()
    DshProfilePluginBridge._validate_source(str(source))

    with pytest.raises(ValidationError, match="local CLI"):
        DshPluginInstallRequest(source=str(source))
    with pytest.raises(ValueError, match="does not exist"):
        DshProfilePluginBridge._validate_source(str(tmp_path / "missing"))
