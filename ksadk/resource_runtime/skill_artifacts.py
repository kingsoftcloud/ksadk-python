"""Host-owned Skill result publication in the existing durable operation ledger.

This is local runtime storage, not a new upload service. Callers must admit the
current resource lease before every read; opaque IDs do not confer authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from pydantic import Field

from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.operation_ledger import OperationConflict, OperationLedger
from ksadk.skills.runtime.artifact_delivery import export_artifacts


class SkillArtifactQuery(PluginContractModel):
    operation_id: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    offset: int = Field(default=0, strict=True, ge=0, le=20 * 1024 * 1024)
    max_bytes: int = Field(default=32768, strict=True, ge=1, le=32768)


class SkillArtifactReceipts:
    def __init__(self, ledger: OperationLedger):
        self.ledger = ledger
        with ledger._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS skill_results ("
                "operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS skill_artifacts ("
                "operation_id TEXT NOT NULL, id TEXT NOT NULL, data BLOB NOT NULL, "
                "PRIMARY KEY(operation_id, id))"
            )

    def _owned(self, operation_id: str, authority_digest: str):
        receipt = self.ledger.lookup(
            operation_id, authority_digest=authority_digest, operation="execute_skills"
        )
        if receipt is None:
            raise OperationConflict("RESOURCE_OPERATION_NOT_FOUND")
        return receipt

    def result(self, operation_id: str, *, authority_digest: str) -> dict | None:
        self._owned(operation_id, authority_digest)
        with self.ledger._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM skill_results WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def publish(
        self,
        operation_id: str,
        *,
        authority_digest: str,
        root: Path,
        paths: list[str],
        status: str,
    ) -> dict:
        """Atomically retain validated bytes and a terminal receipt; never execute.

        root is supplied by the trusted activation owner, not by the Worker result.
        A repeated publication returns the original receipt and does not read paths.
        """
        self._owned(operation_id, authority_digest)
        if status not in {"succeeded", "failed"}:
            raise ValueError("Only confirmed execution outcomes may be published")
        existing = self.result(operation_id, authority_digest=authority_digest)
        if existing is not None:
            return existing
        with tempfile.TemporaryDirectory(prefix="ksadk-artifact-publication-") as temporary:
            archive_path = Path(temporary) / "artifacts.zip"
            export_artifacts(paths, root, archive_path)
            with zipfile.ZipFile(archive_path) as archive, self.ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # Another host publisher may have committed while files were validated.
                previous = connection.execute(
                    "SELECT payload FROM skill_results WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if previous is not None:
                    return json.loads(previous[0])
                state = connection.execute(
                    "SELECT state FROM operations WHERE id=?", (operation_id,)
                ).fetchone()
                if state is None or state[0] not in {"unknown", "accepted_pending"}:
                    raise OperationConflict("RESOURCE_OPERATION_STATE_CONFLICT")
                artifacts = []
                for entry in archive.infolist():
                    data = archive.read(entry)
                    digest = hashlib.sha256(data).hexdigest()
                    artifact_id = hashlib.sha256(
                        (operation_id + "\0" + entry.filename + "\0" + digest).encode()
                    ).hexdigest()
                    artifacts.append(
                        {
                            "artifactId": artifact_id,
                            "name": entry.filename,
                            "size": len(data),
                            "sha256": digest,
                        }
                    )
                    connection.execute(
                        "INSERT INTO skill_artifacts VALUES (?, ?, ?)",
                        (operation_id, artifact_id, data),
                    )
                result = {"status": status, "operationId": operation_id, "artifacts": artifacts}
                connection.execute(
                    "INSERT INTO skill_results VALUES (?, ?)",
                    (operation_id, json.dumps(result, separators=(",", ":"))),
                )
                connection.execute(
                    "UPDATE operations SET state=? WHERE id=?", (status, operation_id)
                )
                return result

    def read(self, arguments: dict, *, authority_digest: str) -> dict:
        query = SkillArtifactQuery.model_validate(arguments)
        result = self.result(query.operation_id, authority_digest=authority_digest)
        artifact = next(
            (
                item
                for item in (result or {}).get("artifacts", [])
                if item["artifactId"] == query.artifact_id
            ),
            None,
        )
        if artifact is None or query.offset > artifact["size"]:
            raise OperationConflict("RESOURCE_ARTIFACT_NOT_FOUND")
        with self.ledger._connect() as connection:
            row = connection.execute(
                "SELECT substr(data, ?, ?) FROM skill_artifacts WHERE operation_id=? AND id=?",
                (query.offset + 1, query.max_bytes, query.operation_id, query.artifact_id),
            ).fetchone()
        if row is None:
            raise OperationConflict("RESOURCE_ARTIFACT_NOT_FOUND")
        following = query.offset + len(row[0])
        return {
            **artifact,
            "operationId": query.operation_id,
            "encoding": "base64",
            "content": base64.b64encode(row[0]).decode("ascii"),
            "offset": query.offset,
            "nextOffset": following if following < artifact["size"] else None,
        }
