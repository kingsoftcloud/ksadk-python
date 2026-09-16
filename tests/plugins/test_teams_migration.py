import hashlib
import json

import pytest

from ksadk.plugins.teams.contracts import Actor
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.migration import export_history, import_history, inspect_history
from ksadk.plugins.teams.store import TeamsStore


def source_store(path):
    store = TeamsStore(path)
    with store.transaction() as tx:
        tx.put(
            "group",
            "group-one",
            {
                "groupId": "group-one",
                "tenantId": "local",
                "ownerSubject": "local-owner",
                "status": "active",
                "revision": 1,
            },
        )
        tx.put(
            "team_run",
            "run-one",
            {"groupId": "group-one", "teamRunId": "run-one", "status": "succeeded", "revision": 1},
        )
        tx.put(
            "member",
            "group-one:leader",
            {"groupId": "group-one", "memberId": "leader", "sessionId": "original-session"},
        )
        tx.event("group-one", "team_run.updated", {"teamRunId": "run-one"})
    return store


def test_export_import_keeps_identity_events_and_is_idempotent(tmp_path):
    source = source_store(tmp_path / "old.sqlite")
    target = TeamsStore(tmp_path / "new.sqlite")
    archive = tmp_path / "backup.json"
    try:
        result = export_history(source, archive, authority_ref="local-authority")
        assert result["runs"] == result["events"] == 1
        args = dict(
            actor=Actor("account", "owner"),
            authority_ref="server-authority",
            import_id="migration-one",
        )
        first = import_history(target, archive, **args)
        assert import_history(target, archive, **args) == first
        with target.transaction() as tx:
            group = tx.get("group", "group-one")
            assert group["status"] == "archived"
            assert group["tenantId"] == "account"
            assert group["legacySource"]["authorityRef"] == "local-authority"
            assert tx.get("member", "group-one:leader")["sessionId"] == "original-session"
        assert target.events("group-one") == source.events("group-one")
        with source.transaction() as tx:
            assert tx.get("group", "group-one")["status"] == "active"
    finally:
        source.close()
        target.close()


def test_active_or_uncertain_work_cannot_be_exported(tmp_path):
    store = source_store(tmp_path / "source.sqlite")
    try:
        with store.transaction() as tx:
            tx.put("delivery", "unknown", {"groupId": "group-one", "status": "uncertain"})
        with pytest.raises(TeamsError, match="结果未确认"):
            export_history(store, tmp_path / "invalid.json", authority_ref="local")
        assert not (tmp_path / "invalid.json").exists()
    finally:
        store.close()


def test_compatible_local_upgrade_backs_up_history_before_binding_new_artifact(tmp_path):
    from pathlib import Path

    from ksadk.plugins.teams.contracts import API_VERSION, PLUGIN_VERSION
    from ksadk.plugins.teams.migration_cli import upgrade_local_history
    from ksadk.plugins.teams.runtime import TEAMS_PLUGIN_ID

    path = tmp_path / "teams.sqlite"
    store = source_store(path)
    old = {
        "pluginId": TEAMS_PLUGIN_ID,
        "pluginDigest": "sha256:" + "a" * 64,
        "apiVersion": API_VERSION,
        "pluginVersion": PLUGIN_VERSION,
    }
    with store.transaction() as tx:
        tx.put("installation", TEAMS_PLUGIN_ID, old)
    original_events = store.events("group-one")
    store.close()
    result = upgrade_local_history(path, authority_ref="local", plugin_digest="sha256:" + "b" * 64)
    backup = Path(result["backupPath"])
    assert (backup / "teams.sqlite").is_file()
    evidence = inspect_history(backup / "history.json")
    assert evidence["payload"]["objects"]["installation"] == [old]
    store = TeamsStore(path)
    try:
        assert store.events("group-one") == original_events
        with store.transaction() as tx:
            assert tx.get("team_run", "run-one")["status"] == "succeeded"
            assert (
                tx.get("installation", TEAMS_PLUGIN_ID)["previousPluginDigest"]
                == old["pluginDigest"]
            )
    finally:
        store.close()
    assert (
        upgrade_local_history(path, authority_ref="local", plugin_digest="sha256:" + "b" * 64)[
            "status"
        ]
        == "already_current"
    )


def test_local_upgrade_refuses_active_work_without_changing_installation(tmp_path):
    from ksadk.plugins.teams.contracts import API_VERSION, PLUGIN_VERSION
    from ksadk.plugins.teams.migration_cli import upgrade_local_history
    from ksadk.plugins.teams.runtime import TEAMS_PLUGIN_ID

    path = tmp_path / "teams.sqlite"
    store = source_store(path)
    old = {
        "pluginId": TEAMS_PLUGIN_ID,
        "pluginDigest": "sha256:" + "a" * 64,
        "apiVersion": API_VERSION,
        "pluginVersion": PLUGIN_VERSION,
    }
    with store.transaction() as tx:
        tx.put("installation", TEAMS_PLUGIN_ID, old)
        tx.put(
            "team_run",
            "run-one",
            {"groupId": "group-one", "teamRunId": "run-one", "status": "running"},
        )
    store.close()
    with pytest.raises(TeamsError, match="在途任务"):
        upgrade_local_history(path, authority_ref="local", plugin_digest="sha256:" + "b" * 64)
    store = TeamsStore(path)
    try:
        with store.transaction() as tx:
            assert tx.get("installation", TEAMS_PLUGIN_ID) == old
    finally:
        store.close()


def test_corrupt_archive_is_rejected_before_import(tmp_path):
    store = source_store(tmp_path / "source.sqlite")
    archive = tmp_path / "backup.json"
    try:
        export_history(store, archive, authority_ref="local")
        value = json.loads(archive.read_text())
        value["payload"]["events"] = []
        archive.write_text(json.dumps(value))
        with pytest.raises(TeamsError, match="校验失败"):
            inspect_history(archive)
    finally:
        store.close()


def test_artifact_bytes_and_hash_survive_transfer(tmp_path):
    source = source_store(tmp_path / "old" / "source.sqlite")
    target = TeamsStore(tmp_path / "new" / "target.sqlite")
    content = b"reviewed result"
    checksum = "sha256:" + hashlib.sha256(content).hexdigest()
    artifact_root = source.path.parent / "artifacts"
    artifact_root.mkdir()
    (artifact_root / checksum[7:]).write_bytes(content)
    try:
        with source.transaction() as tx:
            tx.put(
                "artifact",
                "artifact-one",
                {
                    "groupId": "group-one",
                    "artifactId": "artifact-one",
                    "digest": checksum,
                    "_path": str(artifact_root / checksum[7:]),
                },
            )
        archive = tmp_path / "backup.json"
        export_history(source, archive, authority_ref="local")
        import_history(
            target, archive, actor=Actor("a", "o"), authority_ref="server", import_id="copy"
        )
        with target.transaction() as tx:
            row = tx.get("artifact", "artifact-one")
        from pathlib import Path

        assert Path(row["_path"]).read_bytes() == content
        assert Path(row["_path"]).parent == target.path.parent / "artifacts"
    finally:
        source.close()
        target.close()


def test_backup_cli_reads_wal_consistently_without_mutating_source(tmp_path):
    from ksadk.plugins.teams.migration_cli import backup_history

    source = source_store(tmp_path / "source.sqlite")
    try:
        before = source.path.read_bytes()
        output = tmp_path / "export.json"
        backup_history(source.path, output, authority_ref="local")
        assert source.path.read_bytes() == before
        assert (
            inspect_history(output)["payload"]["objects"]["team_run"][0]["teamRunId"] == "run-one"
        )
    finally:
        source.close()


@pytest.mark.parametrize("corruption", ["records", "kind", "event"])
def test_rehashed_but_inconsistent_archive_is_rejected(tmp_path, corruption):
    from ksadk.plugins.teams.store import digest

    source = source_store(tmp_path / "source.sqlite")
    try:
        archive = tmp_path / "backup.json"
        export_history(source, archive, authority_ref="local")
        envelope = json.loads(archive.read_text())
        payload = envelope["payload"]
        if corruption == "records":
            payload["records"][0][2]["name"] = "not-in-index"
        elif corruption == "kind":
            payload["records"].append(["execution_lease", "scheduler", {"owner": "forged"}])
        else:
            payload["events"][0]["groupSeq"] = 2
        envelope["digest"] = digest(payload)
        archive.write_text(json.dumps(envelope))
        with pytest.raises(TeamsError):
            inspect_history(archive)
    finally:
        source.close()


@pytest.mark.asyncio
async def test_verified_switch_freezes_source_and_is_restart_safe(tmp_path, monkeypatch):
    from ksadk.plugins.teams import migration_cli
    from ksadk.plugins.teams.runtime import TeamsRuntime
    from ksadk.studio.configuration import WorkspaceConfiguration
    from ksadk.studio.workspace import Workspace

    workspace = tmp_path / "workspace"
    source = source_store(workspace / ".agentkit/plugins/teams/teams.sqlite")
    archive = tmp_path / "backup.json"
    result = export_history(source, archive, authority_ref="local")
    source.close()

    class Client:
        def __init__(self, base_url, **kwargs):
            self.base_url = base_url

        async def request(self, method, path):
            return (
                {"digest": result["digest"], "status": "imported_read_only"}
                if path.startswith("/history/")
                else {"authorityRef": "server"}
            )

        async def close(self):
            pass

    monkeypatch.setattr(migration_cli, "TeamsHTTPClient", Client)
    args = (archive, source.path, workspace, "https://teams.example.test/teams", "migration-one")
    first = await migration_cli.activate_history(*args)
    assert await migration_cli.activate_history(*args) == first
    assert (
        WorkspaceConfiguration(Workspace(workspace)).environment()["KSADK_TEAMS_SERVER_URL"]
        == args[3]
    )
    runtime = TeamsRuntime(path=source.path, authority_ref="local", host=object())
    with pytest.raises(TeamsError) as failure:
        await runtime.start(background=False)
    assert failure.value.code == "authority_transferred"
    assert source.path.stat().st_mode & 0o222 == 0
