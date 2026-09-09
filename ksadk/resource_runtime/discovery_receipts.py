"""Host-owned discovery selections in the existing operation ledger.

Receipts pin identity and cache lookup fields, never grant permission or contain
URLs. The runner supplies a stable logical run reference across recovery.
"""
from __future__ import annotations

import hashlib
import json
import re

from pydantic import Field

from ksadk.resource_runtime.contracts import SelectedSkill
from ksadk.resource_runtime.leases import ResourceScope
from ksadk.resource_runtime.operation_ledger import OperationConflict, OperationLedger
from ksadk.skills.models import ContentHash, SkillRef


class DiscoverySkillReceipt(SelectedSkill):
    name: str = Field(strict=True, min_length=1, max_length=256)
    version: str = Field(default="", strict=True, max_length=256)

    @classmethod
    def from_ref(cls, ref: SkillRef):
        return cls(
            skill_id=ref.skill_id, version_id=ref.version_id,
            content_hash=ref.content_hash.render() if ref.content_hash else "",
            name=ref.name, version=ref.version,
        )

    def to_ref(self) -> SkillRef:
        return SkillRef(
            skill_id=self.skill_id, version_id=self.version_id,
            name=self.name, version=self.version,
            content_hash=ContentHash.parse(self.content_hash),
        )


def discovery_scope_key(scope: ResourceScope, run_ref: str) -> str:
    if not isinstance(run_ref, str) or not 1 <= len(run_ref) <= 256:
        raise ValueError("Discovery requires a stable host run reference")
    return hashlib.sha256(json.dumps([
        "discovery-selection-v1", run_ref,
        scope.identity.model_dump(by_alias=True, mode="json"),
        scope.build_digest, scope.binding_snapshot_digest, scope.binding_id,
    ], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DiscoverySelectionReceipts:
    def __init__(self, ledger: OperationLedger):
        self.ledger = ledger
        with ledger._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS discovery_selections ("
                "scope_key TEXT NOT NULL, skill_id TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(scope_key, skill_id))"
            )

    @staticmethod
    def _key(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid discovery scope")
        return value

    def read(self, scope_key: str) -> tuple[DiscoverySkillReceipt, ...]:
        with self.ledger._connect() as connection:
            rows = connection.execute(
                "SELECT skill_id,payload FROM discovery_selections WHERE scope_key=? "
                "ORDER BY skill_id LIMIT 33", (self._key(scope_key),),
            ).fetchall()
        if len(rows) > 32:
            raise OperationConflict("Discovery selection limit exceeded")
        results = tuple(DiscoverySkillReceipt.model_validate_json(row[1]) for row in rows)
        if any(row[0] != result.skill_id for row, result in zip(rows, results)):
            raise OperationConflict("Discovery selection identity mismatch")
        return results

    def record(self, scope_key: str, receipt: DiscoverySkillReceipt) -> None:
        scope_key = self._key(scope_key)
        receipt = DiscoverySkillReceipt.model_validate(receipt.model_dump())
        with self.ledger._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT payload FROM discovery_selections WHERE scope_key=? AND skill_id=?",
                (scope_key, receipt.skill_id),
            ).fetchone()
            if previous:
                if DiscoverySkillReceipt.model_validate_json(previous[0]) != receipt:
                    raise OperationConflict("Discovery selection cannot change within a run")
                return
            count = connection.execute(
                "SELECT COUNT(*) FROM discovery_selections WHERE scope_key=?", (scope_key,),
            ).fetchone()[0]
            if count >= 32:
                raise OperationConflict("Discovery selection limit exceeded")
            connection.execute(
                "INSERT INTO discovery_selections VALUES (?,?,?)",
                (scope_key, receipt.skill_id, receipt.model_dump_json(by_alias=True)),
            )
