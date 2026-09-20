"""Real readonly ZIP/source verifier inputs for Host factory tests."""

import io
import os
import zipfile
from types import SimpleNamespace

from ksadk.plugins.teams.build_artifacts import (
    EffectiveBuildConfiguration,
    LoadedBuildProvider,
    build_archive,
)


def loaded_build(root):
    configuration = EffectiveBuildConfiguration(
        agent_definition={"prompt": "fixed"},
        tools_policy={"tools": []},
        behavior_config={"temperature": 0.7},
        external_mutable_inputs=False,
    )
    archive = build_archive(
        {"entrypoint.py": b"# fixed Code entrypoint\n"},
        entrypoint="entrypoint.py",
        agent_definition=configuration.agent_definition,
        tools_policy=configuration.tools_policy,
        behavior_config=configuration.behavior_config,
        dependency_lock=b"locked==1.0",
    )
    root.mkdir()
    archive_path, workspace = root / "source.zip", root / "code"
    archive_path.write_bytes(archive.archive)
    archive_path.chmod(0o444)
    workspace.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive.archive)) as package:
        for entry in package.infolist():
            path = workspace / entry.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(package.read(entry))
            path.chmod(0o444)
    for directory, _, _ in os.walk(workspace, topdown=False):
        os.chmod(directory, 0o555)
    provider = LoadedBuildProvider(
        archive_path=archive_path,
        workspace=workspace,
        effective_configuration=lambda: configuration,
    )
    return SimpleNamespace(
        provider=provider,
        archive=archive,
        archive_path=archive_path,
        workspace=workspace,
        configuration=configuration,
    )
