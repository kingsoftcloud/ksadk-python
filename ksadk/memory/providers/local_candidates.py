"""Local candidate consumption under the owning Provider's writer transaction.

No candidate body is retained while pending/rejected. Original source markers
outlive a forget so replay cannot count old prose as new evidence or resurrect it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from ksadk.memory.extraction import NATIVE_USER_EXTRACTION_POLICY, derive_profile_slot_key
from ksadk.memory.models import MemoryRecord
from ksadk.memory.policy import MemoryPolicy, content_hash

from .local_sqlite import VersionConflict, _now_iso, _row_to_record


def namespace(tenant_id, workspace_id, scope, scope_id, memory_type):
    identity = (tenant_id, None if scope == "org" else workspace_id, scope, scope_id, memory_type)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def discard_slot_observations(conn, record):
    key = namespace(record.tenant_id, record.workspace_id, record.scope,
                    record.scope_id, record.memory_type)
    slot = str(record.metadata.get("slot_key") or derive_profile_slot_key(record.content))
    conn.execute(
        "DELETE FROM memory_candidate_observations WHERE namespace=? "
        "AND (content_hash=? OR (slot_key<>'' AND slot_key=?))",
        (key, record.content_hash, slot),
    )


def consume_explicit_write_horizon(conn, record, original, candidates):
    """A confirmed write consumes earlier eligible inputs in its actual slot.

This is part of the same writer transaction as the fact and receipt. Keep only
original identities/digests, and preserve unrelated slots for later extraction.
"""
    targets = (original, record) if original is not None else (record,)
    slots = {value for target in targets if (
        value := target.metadata.get("slot_key") or derive_profile_slot_key(target.content)
    )}
    hashes = {target.content_hash for target in targets}
    for candidate, source_digest in candidates:
        if (candidate.scope, candidate.scope_id) != (record.scope, record.scope_id):
            raise VersionConflict("candidate_write_horizon_scope_unbound")
        if not (candidate.slot_key in slots or content_hash(candidate.content) in hashes):
            continue
        key = namespace(record.tenant_id, record.workspace_id, candidate.scope,
                        candidate.scope_id, candidate.memory_type)
        for target in targets:
            conn.execute(
                "DELETE FROM memory_candidate_observations WHERE namespace=? "
                "AND (content_hash=? OR (slot_key<>'' AND slot_key=?))",
                (key, target.content_hash,
                 target.metadata.get("slot_key") or derive_profile_slot_key(target.content)),
            )
        source = candidate.source_event_ids[0]
        previous = conn.execute(
            "SELECT source_digest FROM memory_candidate_sources "
            "WHERE namespace=? AND source_event_id=?", (key, source),
        ).fetchone()
        if previous is not None and previous["source_digest"] != source_digest:
            raise VersionConflict("candidate_source_changed")
        conn.execute("INSERT OR IGNORE INTO memory_candidate_sources VALUES (?,?,?)",
                     (key, source, source_digest))


def consume(provider, candidate, *, operation_key, request_digest, source_session_id,
            source_digest, tenant_id, workspace_id):
    # The caller holds the Provider lock and BEGIN IMMEDIATE. Fact, source,
    # independent observations and receipt commit/roll back together.
    prior = provider._mutation_receipt_locked(operation_key, request_digest)
    if prior is not None:
        return prior
    if tenant_id != provider._tenant_id or len(candidate.source_event_ids) != 1:
        raise VersionConflict("candidate_source_or_tenant_unbound")
    conn, source = provider._conn, candidate.source_event_ids[0]
    key = namespace(tenant_id, workspace_id, candidate.scope, candidate.scope_id,
                    candidate.memory_type)
    fact_hash = content_hash(candidate.content)
    fingerprint = hashlib.sha256(json.dumps(
        (NATIVE_USER_EXTRACTION_POLICY, candidate.slot_key, fact_hash),
    ).encode()).hexdigest()

    def seal(decision, *, reason, observations=0, record=None, action=None):
        result = {"committed": decision == "committed", "decision": decision,
                  "code": reason, "observations": observations}
        if record is not None:
            result.update(memory_id=record.memory_id, version=record.version,
                          status=record.status, content_hash=record.content_hash,
                          action=action)
        conn.execute("INSERT INTO memory_operation_receipts VALUES (?,?,?)",
                     (operation_key, request_digest, json.dumps(result, ensure_ascii=False)))
        return result

    policy = MemoryPolicy()
    early = policy.evaluate(candidate)
    if early.decision == "reject":
        return seal("rejected", reason=early.reason)
    if len(candidate.content) > provider.capabilities().max_record_chars:
        return seal("rejected", reason="candidate_content_over_budget")
    original = conn.execute(
        "SELECT source_digest FROM memory_candidate_sources "
        "WHERE namespace=? AND source_event_id=?",
        (key, source),
    ).fetchone()
    if original is not None:
        if original["source_digest"] != source_digest:
            raise VersionConflict("candidate_source_changed")
        count = conn.execute(
            "SELECT COUNT(*) FROM memory_candidate_observations "
            "WHERE namespace=? AND candidate_key=?",
            (key, fingerprint),
        ).fetchone()[0]
        return seal("duplicate_source", reason="original_source_already_consumed",
                    observations=count)

    # Native writes have a slot key. For older records derive only the same
    # deterministic slot inside the exact tenant/workspace/scope/type partition.
    rows = conn.execute(
        "SELECT * FROM memory_records WHERE tenant_id=? AND scope=? AND scope_id=? "
        "AND memory_type=? AND status='active' "
        "AND (scope='org' OR workspace_id=?)",
        (tenant_id, candidate.scope, candidate.scope_id, candidate.memory_type, workspace_id),
    ).fetchall()
    matches = [record for row in rows if (
        (record := _row_to_record(row)).content_hash == fact_hash
        or bool(candidate.slot_key) and (
            record.metadata.get("slot_key") or derive_profile_slot_key(record.content)
        ) == candidate.slot_key
    )]
    if len(matches) > 1:
        return seal("rejected", reason="candidate_slot_ambiguous")
    existing = matches[0] if matches else None
    if existing is not None and existing.write_policy == "locked":
        return seal("rejected", reason="candidate_slot_locked")
    if candidate.operation != "add" and existing is None:
        return seal("rejected", reason="candidate_correction_target_missing")
    conn.execute("INSERT INTO memory_candidate_sources VALUES (?,?,?)",
                 (key, source, source_digest))
    conn.execute("INSERT INTO memory_candidate_observations VALUES (?,?,?,?,?,?)",
                 (key, fingerprint, source, fact_hash, candidate.slot_key, _now_iso()))
    observed = conn.execute(
        "SELECT source_event_id FROM memory_candidate_observations "
        "WHERE namespace=? AND candidate_key=? ORDER BY source_event_id",
        (key, fingerprint),
    ).fetchall()
    count = len(observed)
    effective = replace(candidate, operation="update") if existing else candidate
    evaluated = policy.evaluate(effective, existing=existing, observations=count)
    if evaluated.decision != "commit":
        return seal(evaluated.decision, reason=evaluated.reason, observations=count)
    if existing is not None and existing.content_hash == fact_hash:
        return seal("duplicate", reason="same_current_fact", observations=count, record=existing)
    from uuid import uuid4

    record = MemoryRecord(
        memory_id=existing.memory_id if existing else "memory_" + uuid4().hex,
        tenant_id=tenant_id, workspace_id=workspace_id, scope=candidate.scope,
        scope_id=candidate.scope_id, memory_type=candidate.memory_type,
        content=candidate.content, summary=candidate.content, status="active",
        confidence=candidate.confidence, importance=candidate.importance,
        valid_from=existing.valid_from if existing else _now_iso(), valid_to="",
        expires_at=candidate.expires_at,
        source_session_id=existing.source_session_id if existing else source_session_id,
        source_event_ids=list(dict.fromkeys((
            *(existing.source_event_ids if existing else ()),
            *(row["source_event_id"] for row in observed),
        ))), source_seq_range=None, content_hash=fact_hash, version=1,
        metadata={**(existing.metadata if existing else {}),
                  "slot_key": candidate.slot_key,
                  "extraction_policy": NATIVE_USER_EXTRACTION_POLICY,
                  "mutation_session_id": source_session_id, "source_input_digest": source_digest},
        write_policy=existing.write_policy if existing else
        "user_confirm" if candidate.reason.startswith("explicit_user") else "auto",
    )
    saved = provider._upsert_locked(record, expected_version=existing.version if existing else None)
    return seal("committed", reason=evaluated.reason, observations=count, record=saved,
                action="correct" if existing else "remember")
