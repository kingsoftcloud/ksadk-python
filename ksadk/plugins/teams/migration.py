"""Checksummed, idempotent transfer of settled Teams history.

Active or uncertain executions must be reconciled by their original authority
before export. This module never edits the source database or its plugin lock.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .contracts import TERMINAL, Actor
from .errors import TeamsError
from .store import KINDS, TeamsStore, digest, encode

FORMAT = "teams-history/v1"
PORTABLE_KINDS = KINDS - {
    "execution_node",
    "execution_command",
    "execution_lease",
    "leader_takeover",
    "teams_import",
}


def _validate_payload(payload):
    records = payload.get("records")
    if not isinstance(records, list):
        raise TeamsError("migration_records_required", "历史包缺少原始对象身份", status=422)
    objects = {kind: [] for kind in sorted(PORTABLE_KINDS)}
    keys = set()
    for record in records:
        if not isinstance(record, (list, tuple)) or len(record) != 3:
            raise TeamsError("migration_record_invalid", "历史对象格式无效", status=422)
        kind, key, value = record
        if (
            kind not in PORTABLE_KINDS
            or not isinstance(key, str)
            or not key
            or not isinstance(value, dict)
        ):
            raise TeamsError("migration_kind_unsupported", "历史包包含不支持的对象类型", status=422)
        if (kind, key) in keys:
            raise TeamsError("migration_duplicate_object", "历史对象身份重复", status=422)
        keys.add((kind, key))
        objects[kind].append(value)
    if payload.get("objects") != objects:
        raise TeamsError("migration_records_mismatch", "历史对象索引与原始记录不一致", status=422)
    group_ids = {group["groupId"] for group in objects["group"]}
    run_ids = {run["teamRunId"] for run in objects["team_run"]}
    run_groups = {run["teamRunId"]: run["groupId"] for run in objects["team_run"]}
    for kind, key, value in records:
        if kind != "installation" and value.get("groupId") not in group_ids:
            raise TeamsError("migration_scope_mismatch", "历史记录引用了包外团队", status=422)
        if value.get("teamRunId") and value["teamRunId"] not in run_ids:
            raise TeamsError("migration_scope_mismatch", "历史记录引用了包外任务", status=422)
        if value.get("teamRunId") and run_groups[value["teamRunId"]] != value.get("groupId"):
            raise TeamsError("migration_scope_mismatch", "任务记录与所属团队不一致", status=422)
        identity_fields = {
            "group": "groupId",
            "team_run": "teamRunId",
            "task": "taskId",
            "message": "messageId",
            "delivery": "deliveryId",
            "artifact": "artifactId",
        }
        if kind in identity_fields and value.get(identity_fields[kind]) != key:
            raise TeamsError("migration_identity_mismatch", "历史对象身份与索引不一致", status=422)
        if kind == "team_run" and value.get("status") not in TERMINAL:
            raise TeamsError("migration_active_runs", "历史包包含在途任务，不能导入", status=409)
        if kind == "delivery" and (
            value.get("status") in {"pending", "uncertain"}
            or (value.get("status") == "accepted" and not value.get("_terminalState"))
        ):
            raise TeamsError("migration_uncertain_execution", "历史包包含未确认的执行", status=409)
        if kind in {"control", "member_control"} and value.get("status") in {
            "pending",
            "barriers_confirmed",
        }:
            raise TeamsError("migration_active_control", "历史包包含尚未完成的控制操作", status=409)
    sequences = payload.get("sequences", [])
    watermarks = dict(sequences)
    if len(watermarks) != len(sequences) or set(watermarks) - group_ids:
        raise TeamsError("migration_events_invalid", "历史事件作用域无效", status=422)
    seen_events, seen_ids = {}, set()
    for event in payload.get("events", []):
        gid, seq, event_id = event.get("groupId"), event.get("groupSeq"), event.get("eventId")
        if gid not in group_ids or event_id in seen_ids or seq != seen_events.get(gid, 0) + 1:
            raise TeamsError("migration_events_invalid", "历史事件重复或不连续", status=422)
        seen_events[gid] = seq
        seen_ids.add(event_id)
    if any(seen_events.get(gid, 0) != watermarks.get(gid, 0) for gid in group_ids):
        raise TeamsError("migration_events_invalid", "历史事件水位不一致", status=422)


def export_history(
    store: TeamsStore, destination: Path, *, authority_ref: str, artifact_root: Path | None = None
) -> dict[str, Any]:
    with store.transaction() as tx:
        runs = tx.list("team_run")
        if any(run["status"] not in TERMINAL for run in runs):
            raise TeamsError(
                "migration_active_runs", "请先停止新派发并核对所有在途任务", status=409
            )
        if any(
            row.get("status") == "uncertain" and not row.get("_terminalState")
            for row in tx.list("delivery")
        ):
            raise TeamsError(
                "migration_uncertain_execution",
                "存在结果未确认的执行，不能复制为新任务",
                status=409,
            )
        # Coordination/node credentials are not portable history.
        objects = {kind: tx.list(kind) for kind in sorted(PORTABLE_KINDS)}
        records = [
            (kind, key, json.loads(body))
            for kind, key, body in tx.connection.execute(
                "SELECT kind,object_id,body FROM team_objects ORDER BY ordinal"
            )
            if kind in PORTABLE_KINDS
        ]
        events = [
            json.loads(row[0])
            for row in tx.connection.execute("SELECT body FROM group_events ORDER BY group_id,seq")
        ]
        sequences = [
            list(row)
            for row in tx.connection.execute(
                "SELECT group_id,watermark FROM group_sequences ORDER BY group_id"
            )
        ]
        receipts = [
            list(row)
            for row in tx.connection.execute(
                "SELECT scope,key,payload_digest,response FROM team_idempotency ORDER BY scope,key"
            )
        ]
    blobs = {}
    for artifact in objects.get("artifact", []):
        checksum = artifact.get("digest", "")
        root = artifact_root or (store.path.parent / "artifacts" if store.path else None)
        if root is None or not checksum.startswith("sha256:") or len(checksum) != 71:
            raise TeamsError("migration_artifact_invalid", "交付物来源或摘要无效", status=422)
        from .artifacts import read_workspace_artifact

        _, data = read_workspace_artifact(root, checksum[7:])
        if "sha256:" + hashlib.sha256(data).hexdigest() != checksum:
            raise TeamsError("migration_artifact_checksum", "交付物摘要不一致", status=422)
        blobs[checksum] = base64.b64encode(data).decode("ascii")
    payload = {
        "format": FORMAT,
        "authorityRef": authority_ref,
        "objects": objects,
        "records": records,
        "blobs": blobs,
        "events": events,
        "sequences": sequences,
        "receipts": receipts,
    }
    _validate_payload(payload)
    envelope = {"payload": payload, "digest": digest(payload)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Explicit, exclusive output: never overwrite a previously verified backup.
    with destination.open("x", encoding="utf-8") as stream:
        os.chmod(destination, 0o600)
        stream.write(encode(envelope))
    return {
        "digest": envelope["digest"],
        "groups": len(objects["group"]),
        "runs": len(runs),
        "events": len(events),
    }


def inspect_history(source: Path) -> dict[str, Any]:
    if source.stat().st_size > 128 * 1024 * 1024:
        raise TeamsError("migration_archive_too_large", "历史包超过单次导入限制", status=422)
    envelope = json.loads(source.read_text(encoding="utf-8"))
    payload = envelope.get("payload", {})
    if payload.get("format") != FORMAT or envelope.get("digest") != digest(payload):
        raise TeamsError("migration_checksum_mismatch", "历史包校验失败", status=422)
    _validate_payload(payload)
    return envelope


def import_history(
    store: TeamsStore,
    source: Path,
    *,
    actor: Actor,
    authority_ref: str,
    import_id: str,
    artifact_root: Path | None = None,
):
    envelope = inspect_history(source)
    payload = envelope["payload"]
    root = artifact_root or (store.path.parent / "artifacts" if store.path else None)
    verified_blobs = {}
    verified_data = {}
    for checksum, content in payload.get("blobs", {}).items():
        data = base64.b64decode(content, validate=True)
        if len(data) > 20 * 1024 * 1024 or "sha256:" + hashlib.sha256(data).hexdigest() != checksum:
            raise TeamsError("migration_artifact_checksum", "交付物摘要不一致", status=422)
        if root is None:
            raise TeamsError(
                "migration_artifact_root_required", "需要服务端交付物存储目录", status=422
            )
        target = root / checksum[7:]
        if target.exists() and (
            target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != checksum[7:]
        ):
            raise TeamsError("migration_artifact_checksum", "目标交付物摘要不一致", status=422)
        verified_blobs[checksum] = target
        verified_data[checksum] = data
    if any(value.get("digest") not in verified_blobs for value in payload["objects"]["artifact"]):
        raise TeamsError("migration_artifact_missing", "历史包缺少交付物", status=422)

    def perform(tx):
        groups = payload["objects"].get("group", [])
        ids = {group["groupId"] for group in groups}
        if any(tx.get("group", group_id, required=False) for group_id in ids):
            raise TeamsError(
                "migration_group_conflict", "目标已存在同标识团队，请核对导入记录", status=409
            )
        # Raw object keys are supplied by the exporter, including compound keys.
        records = payload.get("records")
        if records is None:
            raise TeamsError("migration_records_required", "历史包缺少原始对象身份", status=422)
        if any(
            tx.get(kind, key, required=False) for kind, key, _ in records if kind != "installation"
        ):
            raise TeamsError(
                "migration_object_conflict", "目标已有同标识历史对象，不能覆盖", status=409
            )
        # Only materialize bytes after all identity/scope/collision checks.
        for checksum, target in verified_blobs.items():
            root.mkdir(parents=True, exist_ok=True)
            try:
                with target.open("xb") as stream:
                    stream.write(verified_data[checksum])
            except FileExistsError:
                from .artifacts import read_workspace_artifact

                _, existing = read_workspace_artifact(root, checksum[7:])
                if existing != verified_data[checksum]:
                    raise TeamsError(
                        "migration_artifact_checksum", "目标交付物摘要不一致", status=422
                    )
        for kind, key, original in records:
            if kind == "installation":
                continue
            if kind not in KINDS:
                raise TeamsError(
                    "migration_kind_unsupported", "历史包包含不支持的对象类型", status=422
                )
            value = json.loads(encode(original))
            if value.get("groupId") and value["groupId"] not in ids:
                raise TeamsError("migration_scope_mismatch", "历史记录引用了包外团队", status=422)
            if kind == "group":
                value["legacySource"] = {
                    "authorityRef": payload["authorityRef"],
                    "tenantId": value["tenantId"],
                    "ownerSubject": value["ownerSubject"],
                }
                value.update(
                    tenantId=actor.tenant_id,
                    ownerSubject=actor.subject,
                    authorityRef=authority_ref,
                    status="archived",
                )
            if kind == "artifact":
                if value.get("digest") not in verified_blobs:
                    raise TeamsError("migration_artifact_missing", "历史包缺少交付物", status=422)
                value["_path"] = str(verified_blobs[value["digest"]])
            tx.put(kind, key, value)
        for group_id, watermark in payload["sequences"]:
            tx.connection.execute("INSERT INTO group_sequences VALUES(?,?)", (group_id, watermark))
        for event in payload["events"]:
            tx.connection.execute(
                "INSERT INTO group_events VALUES(?,?,?,?)",
                (event["groupId"], event["groupSeq"], event["eventId"], encode(event)),
            )
        result = {
            "importId": import_id,
            "digest": envelope["digest"],
            "groups": len(groups),
            "events": len(payload["events"]),
            "status": "imported_read_only",
        }
        tx.put(
            "teams_import",
            import_id,
            {
                **result,
                "legacyReceipts": payload["receipts"],
                "legacyAuthorityRef": payload["authorityRef"],
            },
        )
        return result

    return store.mutate(
        f"import:{actor.tenant_id}:{actor.subject}",
        import_id,
        {"digest": envelope["digest"]},
        perform,
    )
