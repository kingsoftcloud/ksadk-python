"""Durable, conservative effect boundary for explicitly controlled Teams tools.

This is an injectable component, not capability advertisement or Host assembly.
The caller supplies real per-tool grant/policy validation and an authoritative
Server prepared ACK. Opaque shell tools are deliberately unsupported. Unknown
means do not replay; even an expired local claim never grants a second send.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from ksadk.plugins.teams.cloud_contracts import (
    canonical_bytes,
    digest,
)
from ksadk.plugins.teams.effect_contracts import (
    EffectOutcome as EffectOutcome,
)
from ksadk.plugins.teams.effect_contracts import (
    EffectPreparedReceipt as EffectPreparedReceipt,
)
from ksadk.plugins.teams.effect_contracts import (
    EffectRecord as EffectRecord,
)
from ksadk.plugins.teams.effect_contracts import (
    EffectReportReceipt as EffectReportReceipt,
)
from ksadk.plugins.teams.effect_contracts import (
    EffectRequest as EffectRequest,
)
from ksadk.plugins.teams.effect_contracts import (
    effect_payload_digest as effect_payload_digest,
)


class EffectConflict(RuntimeError):
    """Safe error code; never includes raw tool arguments or transport errors."""


class EffectUncertain(EffectConflict):
    pass


class EffectJournal(Protocol):
    """Native PostgreSQL adapters implement this port without SQLite wrapping."""

    store_incarnation: str
    journal_incarnation: str

    async def record_invocation(self, request: EffectRequest) -> str: ...
    async def get_invocation(self, request: EffectRequest) -> bool: ...
    async def get(self, request: EffectRequest) -> EffectRecord | None: ...
    async def prepare(self, request: EffectRequest) -> EffectRecord: ...
    async def claim(self, request: EffectRequest, claim_id: str) -> EffectRecord | None: ...
    async def settle(
        self, request: EffectRequest, claim_id: str, outcome: EffectOutcome
    ) -> EffectRecord: ...
    async def unresolved(self, *, limit: int = 100, after: str = "") -> list[EffectRecord]: ...
    async def pending_reports(self, *, limit: int = 100) -> list[EffectRecord]: ...
    async def for_execution(
        self, context_ref: str, native_run_id: str, *, limit: int = 100, after: str = ""
    ) -> list[EffectRecord]: ...
    async def acknowledge_report(
        self, effect_key: str, revision: int, evidence_digest: str
    ) -> None: ...


class SQLiteEffectJournal:
    """Local Host journal; explicitly create once, fail closed on reopen loss.

    Place beside the original Kernel's durable store. A separate journal UUID
    prevents recreated/lost journals from matching an earlier Server ACK, even
    if the caller accidentally initializes using the same Kernel incarnation.
    This local adapter does not claim cross-host shared persistence.
    """

    shared_across_hosts = False

    def __init__(self, path: str | Path, *, store_incarnation: str, initialize: bool = False):
        if not isinstance(store_incarnation, str) or not store_incarnation:
            raise ValueError("store_incarnation_required")
        self.path = Path(path)
        self._lock = threading.RLock()
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        if not self.path.is_file():
            raise EffectConflict("effect_journal_unavailable")
        self.connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=rw",
            uri=True,
            check_same_thread=False,
            timeout=10,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            if initialize:
                self.connection.execute("BEGIN IMMEDIATE")
                try:
                    self.connection.execute(
                        "CREATE TABLE IF NOT EXISTS teams_effect_invocations "
                        "(invocation_key TEXT PRIMARY KEY,invocation_digest TEXT NOT NULL)"
                    )
                    self.connection.execute(
                        "CREATE TABLE IF NOT EXISTS teams_effect_meta (id "
                        "INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT "
                        "NULL,store_incarnation TEXT NOT "
                        "NULL,journal_incarnation TEXT NOT NULL)"
                    )
                    self.connection.execute(
                        "CREATE TABLE IF NOT EXISTS teams_effect_entries "
                        "(effect_key TEXT PRIMARY KEY,request_digest TEXT NOT "
                        "NULL,request_json TEXT NOT NULL,phase TEXT NOT "
                        "NULL,revision INTEGER NOT NULL,claim_id "
                        "TEXT,record_json TEXT NOT NULL)"
                    )
                    self.connection.execute(
                        "CREATE TABLE IF NOT EXISTS teams_effect_reports "
                        "(effect_key TEXT NOT NULL,revision INTEGER NOT "
                        "NULL,evidence_digest TEXT NOT NULL,record_json TEXT "
                        "NOT NULL,acknowledged INTEGER NOT NULL DEFAULT "
                        "0,PRIMARY KEY(effect_key,revision),FOREIGN "
                        "KEY(effect_key) REFERENCES "
                        "teams_effect_entries(effect_key))"
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS teams_effect_pending ON "
                        "teams_effect_reports(acknowledged,effect_key,revision)"
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS teams_effect_evidence_execution "
                        "ON teams_effect_reports("
                        "json_extract(record_json,'$.request.context_ref'),"
                        "json_extract(record_json,'$.request.native_run_id'),effect_key,revision)"
                    )
                    self.connection.execute(
                        "INSERT OR IGNORE INTO teams_effect_meta VALUES(1,1,?,?)",
                        (store_incarnation, "journal_" + uuid4().hex),
                    )
                    self.connection.execute("COMMIT")
                except BaseException:
                    self.connection.execute("ROLLBACK")
                    raise
            row = self.connection.execute("SELECT * FROM teams_effect_meta WHERE id=1").fetchone()
            if row is None or row["version"] != 1 or row["store_incarnation"] != store_incarnation:
                raise EffectConflict("effect_store_incarnation_mismatch")
            self.store_incarnation, self.journal_incarnation = (
                store_incarnation,
                row["journal_incarnation"],
            )
        except BaseException:
            self.connection.close()
            raise

    async def close(self):
        await asyncio.to_thread(self._close)

    def _close(self):
        with self._lock:
            self.connection.close()

    def _request(self, request):
        request = EffectRequest.model_validate(request.model_dump(mode="json"))
        if (
            request.store_incarnation != self.store_incarnation
            or request.journal_incarnation != self.journal_incarnation
        ):
            raise EffectConflict("effect_store_incarnation_mismatch")
        if len(canonical_bytes(request.stable())) > 32768:
            raise EffectConflict("effect_request_too_large")
        return request

    def _row(self, request):
        row = self.connection.execute(
            "SELECT * FROM teams_effect_entries WHERE effect_key=?", (request.effect_key,)
        ).fetchone()
        if row and row["request_digest"] != request.request_digest:
            raise EffectConflict("effect_payload_conflict")
        return row

    @staticmethod
    def _record(row):
        record = EffectRecord.model_validate_json(row["record_json"])
        proof = {
            "requestDigest": record.request.request_digest,
            "phase": record.phase,
            "revision": record.revision,
            "outcome": record.outcome.model_dump(mode="json") if record.outcome else None,
        }
        if digest(proof) != record.evidence_digest:
            raise EffectConflict("effect_evidence_corrupt")
        for column, expected in (
            ("effect_key", record.request.effect_key),
            ("request_digest", record.request.request_digest),
            ("phase", record.phase),
            ("revision", record.revision),
            ("evidence_digest", record.evidence_digest),
        ):
            if column in row.keys() and row[column] != expected:
                raise EffectConflict("effect_evidence_corrupt")
        return record

    def _persist(self, request, phase, revision, *, claim_id=None, outcome=None):
        proof = {
            "requestDigest": request.request_digest,
            "phase": phase,
            "revision": revision,
            "outcome": outcome.model_dump(mode="json") if outcome else None,
        }
        record = EffectRecord(
            request=request,
            phase=phase,
            revision=revision,
            evidence_digest=digest(proof),
            outcome=outcome,
        )
        encoded = canonical_bytes(record.model_dump(mode="json")).decode()
        self.connection.execute(
            "INSERT INTO teams_effect_entries VALUES(?,?,?,?,?,?,?) ON "
            "CONFLICT(effect_key) DO UPDATE SET "
            "phase=excluded.phase,revision=excluded.revision,claim_id=excluded.claim_id,record_json=excluded.record_json",
            (
                request.effect_key,
                request.request_digest,
                canonical_bytes(request.model_dump(mode="json")).decode(),
                phase,
                revision,
                claim_id,
                encoded,
            ),
        )
        self.connection.execute(
            "INSERT INTO "
            "teams_effect_reports(effect_key,revision,evidence_digest,record_json)"
            " VALUES(?,?,?,?)",
            (request.effect_key, revision, record.evidence_digest, encoded),
        )
        return record

    def _transaction(self, operation):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                identity = self.connection.execute(
                    "SELECT * FROM teams_effect_meta WHERE id=1"
                ).fetchone()
                if (
                    identity is None
                    or identity["version"] != 1
                    or identity["store_incarnation"] != self.store_incarnation
                    or identity["journal_incarnation"] != self.journal_incarnation
                ):
                    raise EffectConflict("effect_store_incarnation_mismatch")
                result = operation()
                self.connection.execute("COMMIT")
                return result
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    async def get(self, request):
        request = self._request(request)

        def read():
            row = self._row(request)
            return self._record(row) if row else None

        return await asyncio.to_thread(self._transaction, read)

    def _invocation(self, request, *, insert=False):
        row = self.connection.execute(
            "SELECT invocation_digest FROM teams_effect_invocations WHERE invocation_key=?",
            (request.invocation_key,),
        ).fetchone()
        if row is not None and row[0] != request.invocation_digest:
            raise EffectConflict("effect_invocation_conflict")
        if row is None and insert:
            self.connection.execute(
                "INSERT INTO teams_effect_invocations VALUES(?,?)",
                (request.invocation_key, request.invocation_digest),
            )
        return row is not None

    async def record_invocation(self, request):
        request = self._request(request)
        await asyncio.to_thread(self._transaction, lambda: self._invocation(request, insert=True))
        return request.invocation_digest

    async def get_invocation(self, request):
        request = self._request(request)
        return await asyncio.to_thread(self._transaction, lambda: self._invocation(request))

    async def prepare(self, request):
        request = self._request(request)

        def apply():
            row = self._row(request)
            return self._record(row) if row else self._persist(request, "prepared", 1)

        return await asyncio.to_thread(self._transaction, apply)

    async def claim(self, request, claim_id):
        request = self._request(request)
        if not isinstance(claim_id, str) or not 1 <= len(claim_id) <= 128:
            raise ValueError("effect_claim_id_invalid")

        def apply():
            row = self._row(request)
            if row is None:
                raise EffectConflict("effect_not_prepared")
            previous = self._record(row)
            if previous.phase != "prepared":
                return None
            # Persist uncertainty BEFORE touching the external system. Claims
            # never expire into replay eligibility, even across process death.
            return self._persist(request, "unknown", previous.revision + 1, claim_id=claim_id)

        return await asyncio.to_thread(self._transaction, apply)

    async def settle(self, request, claim_id, outcome):
        request = self._request(request)
        outcome = EffectOutcome.model_validate(outcome.model_dump(mode="json"))

        def apply():
            row = self._row(request)
            if row is None or row["claim_id"] != claim_id:
                raise EffectConflict("effect_claim_mismatch")
            previous = self._record(row)
            if previous.outcome:
                if previous.outcome == outcome:
                    return previous
                raise EffectConflict("effect_outcome_conflict")
            if previous.phase != "unknown":
                raise EffectConflict("effect_phase_conflict")
            return self._persist(
                request, outcome.phase, row["revision"] + 1, claim_id=claim_id, outcome=outcome
            )

        return await asyncio.to_thread(self._transaction, apply)

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("effect_page_limit_invalid")

    async def unresolved(self, *, limit=100, after=""):
        self._limit(limit)

        def read():
            rows = self.connection.execute(
                "SELECT record_json FROM teams_effect_entries WHERE phase IN "
                "('prepared','unknown') AND effect_key>? ORDER BY effect_key "
                "LIMIT ?",
                (after, limit),
            ).fetchall()
            return [self._record(row) for row in rows]

        return await asyncio.to_thread(self._transaction, read)

    async def pending_reports(self, *, limit=100):
        self._limit(limit)

        def read():
            rows = self.connection.execute(
                "SELECT record_json FROM teams_effect_reports WHERE "
                "acknowledged=0 ORDER BY effect_key,revision LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._record(row) for row in rows]

        return await asyncio.to_thread(self._transaction, read)

    async def for_execution(self, context_ref, native_run_id, *, limit=100, after=""):
        self._limit(limit)
        after_key, after_revision = _execution_scope(context_ref, native_run_id, after)

        def read():
            rows = self.connection.execute(
                "SELECT * FROM teams_effect_reports WHERE "
                "json_extract(record_json,'$.request.context_ref')=? AND "
                "json_extract(record_json,'$.request.native_run_id')=? AND "
                "(effect_key>? OR (effect_key=? AND revision>?)) "
                "ORDER BY effect_key,revision LIMIT ?",
                (context_ref, native_run_id, after_key, after_key, after_revision, limit),
            ).fetchall()
            result = [self._record(row) for row in rows]
            for record in result:
                self._request(record.request)
            return result

        return await asyncio.to_thread(self._transaction, read)

    async def acknowledge_report(self, effect_key, revision, evidence_digest):
        def apply():
            row = self.connection.execute(
                "SELECT evidence_digest FROM teams_effect_reports WHERE "
                "effect_key=? AND revision=?",
                (effect_key, revision),
            ).fetchone()
            if row is None or row[0] != evidence_digest:
                raise EffectConflict("effect_report_ack_mismatch")
            self.connection.execute(
                "UPDATE teams_effect_reports SET acknowledged=1 WHERE effect_key=? AND revision=?",
                (effect_key, revision),
            )

        await asyncio.to_thread(self._transaction, apply)


def _execution_scope(context_ref, native_run_id, after):
    if (
        not all(
            isinstance(value, str) and 1 <= len(value) <= 256
            for value in (context_ref, native_run_id)
        )
        or not isinstance(after, str)
        or len(after) > 256
    ):
        raise ValueError("effect_execution_scope_invalid")
    if not after:
        return "", 0
    parsed = re.fullmatch(r"(effect_[0-9a-f]{64}):([1-3])", after)
    if parsed is None:
        raise ValueError("effect_execution_cursor_invalid")
    return parsed[1], int(parsed[2])


class PostgresEffectJournal:
    """Native asyncpg journal in the original Kernel pool/schema and tenant.

    ``open(initialize=True)`` is explicit schema provisioning, never a fallback
    for a missing ledger. Runtime reopen verifies both original store identity
    and journal incarnation on every transaction. This adapter owns no pool.
    """

    shared_across_hosts = True
    _request = SQLiteEffectJournal._request
    _record = staticmethod(SQLiteEffectJournal._record)
    _limit = staticmethod(SQLiteEffectJournal._limit)

    def __init__(self, kernel_store, *, store_incarnation):
        from ksadk.kernel.postgres_store import PostgresAgentKernelStore

        if not isinstance(kernel_store, PostgresAgentKernelStore):
            raise TypeError("original PostgreSQL Kernel store required")
        self.kernel_store = kernel_store
        self.namespace = kernel_store.tenant_id
        self.store_incarnation = store_incarnation
        self.journal_incarnation = None

    @classmethod
    async def open(cls, kernel_store, *, store_incarnation, initialize=False):
        self = cls(kernel_store, store_incarnation=store_incarnation)
        async with self._transaction(check_journal=False) as db:
            if initialize:
                for statement in (
                    "CREATE TABLE IF NOT EXISTS teams_effect_pg_meta (namespace TEXT PRIMARY KEY,"
                    "version INTEGER NOT NULL,store_incarnation TEXT NOT NULL,"
                    "journal_incarnation TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS teams_effect_pg_invocations "
                    "(namespace TEXT NOT NULL,invocation_key TEXT NOT NULL,"
                    "invocation_digest TEXT NOT NULL,PRIMARY KEY(namespace,invocation_key))",
                    "CREATE TABLE IF NOT EXISTS teams_effect_pg_entries (namespace TEXT NOT NULL,"
                    "effect_key TEXT NOT NULL,request_digest TEXT NOT NULL,phase TEXT NOT NULL,"
                    "revision BIGINT NOT NULL,claim_id TEXT,record_json TEXT NOT NULL,"
                    "PRIMARY KEY(namespace,effect_key))",
                    "CREATE TABLE IF NOT EXISTS teams_effect_pg_reports (namespace TEXT NOT NULL,"
                    "effect_key TEXT NOT NULL,revision BIGINT NOT NULL,"
                    "evidence_digest TEXT NOT NULL,"
                    "record_json TEXT NOT NULL,acknowledged BOOLEAN NOT NULL DEFAULT FALSE,"
                    "PRIMARY KEY(namespace,effect_key,revision),FOREIGN KEY(namespace,effect_key) "
                    "REFERENCES teams_effect_pg_entries(namespace,effect_key))",
                    "CREATE INDEX IF NOT EXISTS teams_effect_pg_pending ON teams_effect_pg_reports "
                    "(namespace,acknowledged,effect_key,revision)",
                    "CREATE INDEX IF NOT EXISTS teams_effect_pg_evidence_execution "
                    "ON teams_effect_pg_reports "
                    "(namespace, ((record_json::jsonb)->'request'->>'context_ref'),"
                    "((record_json::jsonb)->'request'->>'native_run_id'),effect_key,revision)",
                ):
                    await db.execute(statement)
                await db.execute(
                    "INSERT INTO teams_effect_pg_meta VALUES($1,1,$2,$3) "
                    "ON CONFLICT(namespace) DO NOTHING",
                    self.namespace,
                    store_incarnation,
                    "journal_" + uuid4().hex,
                )
            await self._identity(db)
        return self

    async def _identity(self, db):
        row = await db.fetchrow(
            "SELECT * FROM teams_effect_pg_meta WHERE namespace=$1", self.namespace
        )
        if (
            row is None
            or row["version"] != 1
            or row["store_incarnation"] != self.store_incarnation
            or self.journal_incarnation not in {None, row["journal_incarnation"]}
        ):
            raise EffectConflict("effect_store_incarnation_mismatch")
        self.journal_incarnation = row["journal_incarnation"]

    @asynccontextmanager
    async def _transaction(self, *, check_journal=True):
        async with self.kernel_store._connection() as db, db.transaction():
            await db.execute("SET LOCAL lock_timeout='5s'")
            await db.execute("SET LOCAL statement_timeout='10s'")
            # No lease expiry can turn an unknown effect into a fresh sender.
            # Advisory locking serializes per-tenant initialize and transitions
            # across independent pods while retaining original asyncpg storage.
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                "teams-effect-journal:" + self.namespace,
            )
            await db.execute("SELECT 1 FROM kernel_inbox LIMIT 1")
            row = await db.fetchrow(
                "SELECT incarnation FROM teams_host_store_identity WHERE namespace=$1",
                self.namespace,
            )
            if row is None or row["incarnation"] != self.store_incarnation:
                raise EffectConflict("effect_store_incarnation_mismatch")
            if check_journal:
                await self._identity(db)
            yield db

    async def close(self):
        # Pool lifecycle belongs to the original Kernel.
        return None

    async def _row(self, db, request):
        row = await db.fetchrow(
            "SELECT * FROM teams_effect_pg_entries WHERE namespace=$1 AND effect_key=$2",
            self.namespace,
            request.effect_key,
        )
        if row and row["request_digest"] != request.request_digest:
            raise EffectConflict("effect_payload_conflict")
        return row

    async def _invocation(self, db, request, *, insert=False):
        row = await db.fetchrow(
            "SELECT invocation_digest FROM teams_effect_pg_invocations "
            "WHERE namespace=$1 AND invocation_key=$2",
            self.namespace,
            request.invocation_key,
        )
        if row and row["invocation_digest"] != request.invocation_digest:
            raise EffectConflict("effect_invocation_conflict")
        if row is None and insert:
            await db.execute(
                "INSERT INTO teams_effect_pg_invocations VALUES($1,$2,$3)",
                self.namespace,
                request.invocation_key,
                request.invocation_digest,
            )
        return row is not None

    async def record_invocation(self, request):
        request = self._request(request)
        async with self._transaction() as db:
            await self._invocation(db, request, insert=True)
        return request.invocation_digest

    async def get_invocation(self, request):
        request = self._request(request)
        async with self._transaction() as db:
            return await self._invocation(db, request)

    async def _persist(self, db, request, phase, revision, *, claim_id=None, outcome=None):
        proof = {
            "requestDigest": request.request_digest,
            "phase": phase,
            "revision": revision,
            "outcome": outcome.model_dump(mode="json") if outcome else None,
        }
        record = EffectRecord(
            request=request,
            phase=phase,
            revision=revision,
            evidence_digest=digest(proof),
            outcome=outcome,
        )
        encoded = canonical_bytes(record.model_dump(mode="json")).decode()
        await db.execute(
            "INSERT INTO teams_effect_pg_entries VALUES($1,$2,$3,$4,$5,$6,$7) "
            "ON CONFLICT(namespace,effect_key) DO UPDATE SET phase=excluded.phase,"
            "revision=excluded.revision,claim_id=excluded.claim_id,record_json=excluded.record_json",
            self.namespace,
            request.effect_key,
            request.request_digest,
            phase,
            revision,
            claim_id,
            encoded,
        )
        await db.execute(
            "INSERT INTO teams_effect_pg_reports"
            "(namespace,effect_key,revision,evidence_digest,record_json) VALUES($1,$2,$3,$4,$5)",
            self.namespace,
            request.effect_key,
            revision,
            record.evidence_digest,
            encoded,
        )
        return record

    async def get(self, request):
        request = self._request(request)
        async with self._transaction() as db:
            row = await self._row(db, request)
            return self._record(row) if row else None

    async def prepare(self, request):
        request = self._request(request)
        async with self._transaction() as db:
            row = await self._row(db, request)
            return self._record(row) if row else await self._persist(db, request, "prepared", 1)

    async def claim(self, request, claim_id):
        request = self._request(request)
        if not isinstance(claim_id, str) or not 1 <= len(claim_id) <= 128:
            raise ValueError("effect_claim_id_invalid")
        async with self._transaction() as db:
            row = await self._row(db, request)
            if row is None:
                raise EffectConflict("effect_not_prepared")
            previous = self._record(row)
            if previous.phase != "prepared":
                return None
            return await self._persist(
                db, request, "unknown", previous.revision + 1, claim_id=claim_id
            )

    async def settle(self, request, claim_id, outcome):
        request = self._request(request)
        outcome = EffectOutcome.model_validate(outcome)
        async with self._transaction() as db:
            row = await self._row(db, request)
            if row is None or row["claim_id"] != claim_id:
                raise EffectConflict("effect_claim_mismatch")
            previous = self._record(row)
            if previous.outcome:
                if previous.outcome == outcome:
                    return previous
                raise EffectConflict("effect_outcome_conflict")
            if previous.phase != "unknown":
                raise EffectConflict("effect_phase_conflict")
            return await self._persist(
                db,
                request,
                outcome.phase,
                previous.revision + 1,
                claim_id=claim_id,
                outcome=outcome,
            )

    async def unresolved(self, *, limit=100, after=""):
        self._limit(limit)
        async with self._transaction() as db:
            rows = await db.fetch(
                "SELECT * FROM teams_effect_pg_entries WHERE namespace=$1 "
                "AND phase IN ('prepared','unknown') AND effect_key>$2 "
                "ORDER BY effect_key LIMIT $3",
                self.namespace,
                after,
                limit,
            )
            return [self._record(row) for row in rows]

    async def pending_reports(self, *, limit=100):
        self._limit(limit)
        async with self._transaction() as db:
            rows = await db.fetch(
                "SELECT * FROM teams_effect_pg_reports WHERE namespace=$1 "
                "AND acknowledged=FALSE ORDER BY effect_key,revision LIMIT $2",
                self.namespace,
                limit,
            )
            return [self._record(row) for row in rows]

    async def for_execution(self, context_ref, native_run_id, *, limit=100, after=""):
        self._limit(limit)
        after_key, after_revision = _execution_scope(context_ref, native_run_id, after)
        async with self._transaction() as db:
            rows = await db.fetch(
                "SELECT * FROM teams_effect_pg_reports WHERE namespace=$1 AND "
                "record_json::jsonb->'request'->>'context_ref'=$2 AND "
                "record_json::jsonb->'request'->>'native_run_id'=$3 AND "
                "(effect_key>$4 OR (effect_key=$4 AND revision>$5)) "
                "ORDER BY effect_key,revision LIMIT $6",
                self.namespace,
                context_ref,
                native_run_id,
                after_key,
                after_revision,
                limit,
            )
            result = [self._record(row) for row in rows]
            for record in result:
                self._request(record.request)
            return result

    async def acknowledge_report(self, effect_key, revision, evidence_digest):
        async with self._transaction() as db:
            row = await db.fetchrow(
                "SELECT evidence_digest FROM teams_effect_pg_reports "
                "WHERE namespace=$1 AND effect_key=$2 AND revision=$3",
                self.namespace,
                effect_key,
                revision,
            )
            if row is None or row["evidence_digest"] != evidence_digest:
                raise EffectConflict("effect_report_ack_mismatch")
            await db.execute(
                "UPDATE teams_effect_pg_reports SET acknowledged=TRUE "
                "WHERE namespace=$1 AND effect_key=$2 AND revision=$3",
                self.namespace,
                effect_key,
                revision,
            )


class EffectAdapter(Protocol):
    async def execute(self, arguments: dict[str, Any], *, effect_key: str) -> EffectOutcome: ...


async def drain_effect_reports(
    journal: EffectJournal,
    report_remote: Callable[[EffectRecord], Awaitable[EffectReportReceipt]],
    *,
    limit: int = 100,
    timeout_seconds: float = 30,
) -> int:
    """One bounded, cancellable drain; uncertain ACKs remain pending for replay.

    The transport authenticates the original Host and chooses its live/recovery
    carrier. Acknowledgements cannot delete other effects or evidence revisions.
    The persisted Server receipt, not a newly generated success, must be returned
    after a lost ACK. Callers choose backoff; this function never spins forever.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("effect_page_limit_invalid")
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 60:
        raise ValueError("effect_drain_timeout_invalid")
    acknowledged = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            for record in await journal.pending_reports(limit=limit):
                ack = EffectReportReceipt.model_validate(await report_remote(record))
                if (
                    ack.effectKey != record.request.effect_key
                    or ack.journalRevision != record.revision
                    or ack.evidenceDigest != record.evidence_digest
                ):
                    raise EffectConflict("effect_report_ack_mismatch")
                await journal.acknowledge_report(
                    ack.effectKey, ack.journalRevision, ack.evidenceDigest
                )
                acknowledged += 1
    except (EffectConflict, asyncio.CancelledError):
        raise
    except Exception:
        raise EffectUncertain("effect_report_unconfirmed") from None
    return acknowledged


class EffectExecutor:
    """Grant-gated, write-ahead wrapper. Returns proof, never raw tool secrets.

    ``authorize`` MUST resolve the live original grant/policy at the real tool
    boundary. ``prepare_remote`` MUST return the Server's persisted same-scope
    receipt; its transport must lookup the original key after ACK uncertainty.
    Runtime assembly must not advertise effectLedger until these are mounted.
    """

    def __init__(
        self,
        journal: EffectJournal,
        *,
        authorize: Callable[[EffectRequest], Awaitable[None]],
        prepare_remote: Callable[[EffectRequest], Awaitable[EffectPreparedReceipt]],
    ):
        self.journal, self.authorize, self.prepare_remote = journal, authorize, prepare_remote

    async def invoke(
        self, request: EffectRequest, arguments: dict[str, Any], adapter: EffectAdapter
    ) -> EffectRecord:
        request = EffectRequest.model_validate(request.model_dump(mode="json"))
        arguments = json.loads(canonical_bytes(arguments))
        if (
            not isinstance(arguments, dict)
            or effect_payload_digest(request.tool_name, request.adapter_version, arguments)
            != request.payload_digest
        ):
            raise EffectConflict("effect_payload_conflict")
        # Resolve caller scope before exposing even a cached/uncertain record.
        await self.authorize(request)
        await self.journal.record_invocation(request)
        previous = await self.journal.get(request)
        if previous and previous.phase in {"completed", "failed"}:
            return previous
        if previous and previous.phase == "unknown":
            raise EffectUncertain("effect_unknown_requires_reconciliation")
        await self.journal.prepare(request)
        try:
            ack = EffectPreparedReceipt.model_validate(
                (await self.prepare_remote(request)).model_dump(mode="json")
            )
            if (
                ack.effect_key != request.effect_key
                or ack.payload_digest != request.payload_digest
                or ack.request_digest != request.request_digest
            ):
                raise EffectConflict("effect_prepare_ack_mismatch")
            if ack.phase != "prepared":
                raise EffectUncertain("effect_remote_requires_reconciliation")
        except (EffectConflict, asyncio.CancelledError):
            raise
        except Exception:
            raise EffectUncertain("effect_prepare_unconfirmed") from None
        await self.authorize(request)
        claim_id = uuid4().hex
        record = await self.journal.claim(request, claim_id)
        if record is None:
            raise EffectUncertain("effect_already_claimed")
        try:
            # A claim may wait on storage; recheck immediately before external I/O.
            await self.authorize(request)
            outcome = await adapter.execute(arguments, effect_key=request.effect_key)
            return await self.journal.settle(request, claim_id, outcome)
        except asyncio.CancelledError:
            # Durable unknown survives cancellation, including after remote send.
            raise
        except Exception:
            raise EffectUncertain("effect_execution_uncertain") from None
