from pathlib import Path

from ksadk.studio.configuration import WorkspaceConfiguration
from ksadk.studio.workspace import Workspace


def test_workspace_configuration_reads_global_default_without_overriding_project(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = Workspace(tmp_path / "project")
    workspace.initialize()
    monkeypatch.setattr(
        "ksadk.studio.configuration.get_env_from_global_config",
        lambda: {"AGENTENGINE_REGION": "global-region", "KSYUN_ACCESS_KEY": "global-ak"},
    )
    config = WorkspaceConfiguration(workspace, overrides={})
    assert config.resolve("AGENTENGINE_REGION") == ("global-region", "global")
    config.update_settings({"cloudRegion": "project-region"})
    assert config.resolve("AGENTENGINE_REGION") == ("project-region", "workspace")
