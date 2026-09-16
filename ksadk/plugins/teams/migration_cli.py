"""Read-only source backup and validated history upload; never edits plugin locks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .errors import TeamsError
from .migration import export_history, inspect_history
from .store import TeamsStore, Transaction, now
from .transport import TeamsHTTPClient


class ReadOnlyHistory:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.connection.execute("PRAGMA query_only=ON")

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN")
        try:
            yield Transaction(self.connection)
        finally:
            self.connection.rollback()

    def close(self):
        self.connection.close()


def backup_history(database, destination, *, authority_ref, artifact_root=None):
    """Take a SQLite-consistent snapshot under the same authority maintenance lock."""
    import fcntl

    source = ReadOnlyHistory(database)
    lock = source.path.with_suffix(".authority.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TeamsError("authority_in_use", "请先关闭原团队宿主再备份", status=409) from error
        # Backup covers WAL pages too. Validate/export only this immutable point
        # in time, with original artifact root; never change source schema.
        with tempfile.TemporaryDirectory(prefix="teams-history-") as directory:
            snapshot = Path(directory) / "snapshot.sqlite"
            copied = sqlite3.connect(snapshot)
            try:
                source.connection.backup(copied)
            finally:
                copied.close()
            store = ReadOnlyHistory(snapshot)
            try:
                return export_history(
                    store,
                    Path(destination),
                    authority_ref=authority_ref,
                    artifact_root=artifact_root or source.path.parent / "artifacts",
                )
            finally:
                store.close()
    finally:
        lock.close()
        source.close()


def upgrade_local_history(database, *, authority_ref, plugin_digest):
    """Backup and validate settled v1 history before binding a compatible artifact.

    This is an explicit local repair, separate from the server authority transfer.
    It cannot bless another API/schema generation or unknown active executions.
    """
    import fcntl
    import re
    from uuid import uuid4

    from .contracts import API_VERSION, PLUGIN_VERSION
    from .runtime import TEAMS_PLUGIN_ID

    database = Path(database).resolve(strict=True)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", plugin_digest):
        raise TeamsError("artifact_required", "需要已验证的当前插件制品", status=409)
    with database.with_suffix(".authority.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TeamsError(
                "authority_in_use", "原团队宿主仍在运行，不能升级历史", status=409
            ) from error
        source = ReadOnlyHistory(database)
        try:
            with source.transaction() as tx:
                installed = tx.get("installation", TEAMS_PLUGIN_ID, required=False)
                transferred = tx.get("installation", "teams:authority-transfer", required=False)
                schema = tx.connection.execute(
                    "SELECT value FROM teams_meta WHERE key='schema_version'"
                ).fetchone()
                if transferred:
                    raise TeamsError(
                        "authority_transferred",
                        "已迁往服务端的归档不能在本地升级为可写",
                        status=409,
                    )
                if (
                    not installed
                    or installed.get("apiVersion") != API_VERSION
                    or installed.get("pluginVersion") != PLUGIN_VERSION
                    or not schema
                    or schema[0] != "1"
                ):
                    raise TeamsError(
                        "local_upgrade_unsupported",
                        "此历史版本需要专门迁移，不能直接兼容升级",
                        status=409,
                    )
                if installed["pluginDigest"] == plugin_digest:
                    return {"status": "already_current"}
            backup = database.parent / "backups" / ("compatible-upgrade-" + uuid4().hex)
            backup.mkdir(parents=True, mode=0o700)
            snapshot = backup / "teams.sqlite"
            copied = sqlite3.connect(snapshot)
            try:
                source.connection.backup(copied)
            finally:
                copied.close()
            snapshot.chmod(0o600)
            archive = backup / "history.json"
            evidence = export_history(
                source,
                archive,
                authority_ref=authority_ref,
                artifact_root=database.parent / "artifacts",
            )
            inspect_history(archive)
        finally:
            source.close()
        target = TeamsStore(database)
        try:
            with target.transaction() as tx:
                if tx.get("installation", TEAMS_PLUGIN_ID) != installed:
                    raise TeamsError(
                        "migration_source_changed", "历史状态已改变，请重新检查", status=409
                    )
                tx.put(
                    "installation",
                    TEAMS_PLUGIN_ID,
                    {
                        **installed,
                        "pluginDigest": plugin_digest,
                        "previousPluginDigest": installed["pluginDigest"],
                        "backupDigest": evidence["digest"],
                        "backupPath": str(backup),
                        "upgradedAt": now(),
                    },
                )
        finally:
            target.close()
        return {"status": "upgraded", "backupPath": str(backup), **evidence}


async def upload_history(path, server_url, import_id):
    envelope = inspect_history(Path(path))
    token = os.environ.get("KSADK_TEAMS_ACCESS_TOKEN")
    if not token:
        raise TeamsError(
            "teams_credentials_required", "上传需要环境变量 KSADK_TEAMS_ACCESS_TOKEN", status=401
        )
    client = TeamsHTTPClient(server_url, access_token=token)
    try:
        return await client.request(
            "POST", "/history/import", {"importId": import_id, "archive": envelope}
        )
    finally:
        await client.close()


async def activate_history(path, database, workspace, server_url, import_id):
    """Switch only after verifying import receipt and unchanged settled source."""
    import fcntl

    from ksadk.studio.configuration import WorkspaceConfiguration
    from ksadk.studio.workspace import Workspace

    envelope = inspect_history(Path(path))
    database, workspace = Path(database).resolve(strict=True), Path(workspace).resolve(strict=True)
    if database != workspace / ".agentkit/plugins/teams/teams.sqlite":
        raise TeamsError(
            "migration_workspace_mismatch", "数据库不属于所选 Studio 工作区", status=422
        )
    client = TeamsHTTPClient(server_url, access_token=os.environ.get("KSADK_TEAMS_ACCESS_TOKEN"))
    try:
        imported = await client.request("GET", "/history/import/" + import_id)
        if (
            imported.get("digest") != envelope["digest"]
            or imported.get("status") != "imported_read_only"
        ):
            raise TeamsError("migration_receipt_mismatch", "服务端导入回执与备份不一致", status=409)
        authority = await client.request("GET", "/lifecycle")
        with database.with_suffix(".authority.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise TeamsError(
                    "authority_in_use", "请先关闭原团队宿主再切换", status=409
                ) from error
            source = ReadOnlyHistory(database)
            try:
                with source.transaction() as tx:
                    marker = tx.get("installation", "teams:authority-transfer", required=False)
                if marker and (
                    marker["digest"] != envelope["digest"] or marker["serverUrl"] != client.base_url
                ):
                    raise TeamsError(
                        "authority_already_transferred",
                        "本地历史已迁至另一权威，不能再次切换",
                        status=409,
                    )
                if marker is None:
                    with tempfile.TemporaryDirectory(prefix="teams-switch-") as temporary:
                        current = export_history(
                            source,
                            Path(temporary) / "verify.json",
                            authority_ref=envelope["payload"]["authorityRef"],
                        )
                        if current["digest"] != envelope["digest"]:
                            raise TeamsError(
                                "migration_source_changed",
                                "原库在备份后发生变化，请重新核对备份",
                                status=409,
                            )
            finally:
                source.close()
            if marker is None:
                store = TeamsStore(database)
                try:
                    with store.transaction() as tx:
                        tx.put(
                            "installation",
                            "teams:authority-transfer",
                            {
                                "digest": envelope["digest"],
                                "serverUrl": client.base_url,
                                "authorityRef": authority["authorityRef"],
                                "importId": import_id,
                                "switchedAt": now(),
                                "readOnly": True,
                            },
                        )
                    store._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    store.close()
                os.chmod(database, 0o400)
            WorkspaceConfiguration(Workspace(workspace)).update_settings(
                {"teamsServerUrl": client.base_url}
            )
        return {
            "status": "server_authority_active",
            "authorityRef": authority["authorityRef"],
            "importId": import_id,
            "sourceReadOnly": True,
        }
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="备份、校验或幂等上传已结算的 Agent Teams 历史")
    commands = parser.add_subparsers(dest="action", required=True)
    export = commands.add_parser("export")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--authority-ref", required=True)
    export.add_argument("--artifact-root", type=Path)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("archive", type=Path)
    upload = commands.add_parser("upload")
    upload.add_argument("archive", type=Path)
    upload.add_argument("--server-url", required=True)
    upload.add_argument("--import-id", required=True)
    activate = commands.add_parser("activate")
    activate.add_argument("archive", type=Path)
    activate.add_argument("--database", type=Path, required=True)
    activate.add_argument("--workspace", type=Path, required=True)
    activate.add_argument("--server-url", required=True)
    activate.add_argument("--import-id", required=True)
    args = parser.parse_args()
    try:
        if args.action == "export":
            result = backup_history(
                args.database,
                args.output,
                authority_ref=args.authority_ref,
                artifact_root=args.artifact_root,
            )
        elif args.action == "inspect":
            envelope = inspect_history(args.archive)
            result = {
                "digest": envelope["digest"],
                "status": "verified",
                "groups": len(envelope["payload"]["objects"]["group"]),
            }
        elif args.action == "upload":
            result = asyncio.run(upload_history(args.archive, args.server_url, args.import_id))
        else:
            result = asyncio.run(
                activate_history(
                    args.archive, args.database, args.workspace, args.server_url, args.import_id
                )
            )
        print(json.dumps(result, ensure_ascii=False))
    except TeamsError as error:
        parser.exit(1, f"{error.code}: {error}\n")


if __name__ == "__main__":
    main()
