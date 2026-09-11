import json
from dataclasses import replace
from pathlib import Path

from ksadk.plugins.bridges.dsh import (
    DshPluginInventory,
    DshProfileBuildSnapshot,
    DshProfileProjection,
)
from ksadk.plugins.companion_artifacts import companion_artifact


def test_artifact_binds_actual_locked_graph_and_companion_source(tmp_path):
    source = tmp_path / "application.py"
    source.write_text("version = 1\n")
    snapshot = DshProfileBuildSnapshot(
        projection=DshProfileProjection(
            profile="web", bundles=("@example/tasks",), config_digest="config-one",
            config_bytes=1, host_version="0.1.5-rc.1",
        ),
        dependency_lock_digest="lock-one", installation_digest="bytes-one",
    )
    inventory = [DshPluginInventory(
        profile="web", name="@example/tasks", display_name="Tasks", version="0.1.0",
        requested_spec="@example/tasks@0.1.0", source_digest="source-one", enabled=True,
    )]

    def capture():
        return companion_artifact(
            plugin_id="example", components={"tasks": "@example/tasks"},
            snapshot=snapshot, inventory=inventory, sources=(source,),
        )

    artifact = capture()
    assert artifact == capture()
    relocated = tmp_path / "relocated" / source.name
    relocated.parent.mkdir()
    relocated.write_bytes(source.read_bytes())
    assert companion_artifact(
        plugin_id="example", components={"tasks": "@example/tasks"},
        snapshot=snapshot, inventory=inventory, sources=(relocated,),
    ) == artifact
    for field in ("dependency_lock_digest", "installation_digest", "source_digest"):
        assert replace(artifact, **{field: "changed"}).plugin_digest != artifact.plugin_digest
    source.write_text("version = 2\n")
    assert capture().plugin_digest != artifact.plugin_digest
    inventory[0] = inventory[0].model_copy(update={"version": "0.2.0"})
    assert capture().components[0].version == "0.2.0"


def test_shipped_cordis_tool_contract_matches_authoritative_companion():
    from ksadk.plugins.dsh_teams import TEAMS_OPERATIONS
    from ksadk.plugins.teams.application import TeamsApplication

    path = Path(__file__).parents[2] / "ksadk/plugins/providers/bundles/dsh-teams-tools/tools.json"
    actual = json.loads(path.read_text())
    expected = [
        {"name": name, "description": description, "parameters": schema}
        for name, description, schema in TeamsApplication.tool_definitions()
    ]
    assert actual == expected
    assert {row["name"] for row in actual} == TEAMS_OPERATIONS
