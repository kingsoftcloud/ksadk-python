from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from ksadk.kernel.teams_effects import (
    EffectConflict,
    EffectExecutor,
    EffectOutcome,
    EffectPreparedReceipt,
    EffectRecord,
    EffectReportReceipt,
    EffectRequest,
    EffectUncertain,
    PostgresEffectJournal,
    SQLiteEffectJournal,
    drain_effect_reports,
    effect_payload_digest,
)
from ksadk.plugins.teams.cloud_contracts import TeamsExecutionRef, digest
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres

FIXTURE = Path(__file__).parents[1] / "contracts/fixtures/teams-cloud-v1.json"
REF = next(
    item["value"]
    for item in json.loads(FIXTURE.read_text())["valid"]
    if item["model"] == "execution-reference"
)
ARGS = {"target": "disposable-counter", "increment": 1}


def request(journal, **changes):
    fields = dict(
        ref=TeamsExecutionRef.model_validate(REF),
        context_ref="context-a",
        store_incarnation=journal.store_incarnation,
        journal_incarnation=journal.journal_incarnation,
        native_run_id="native-run",
        tool_call_id="call-a",
        effect_index=0,
        tool_name="counter_increment",
        adapter_version="counter/v1",
        effect_class="external_reconcilable",
        payload_digest=effect_payload_digest("counter_increment", "counter/v1", ARGS),
    )
    fields.update(changes)
    return EffectRequest(**fields)


async def authorize(_):
    return None


async def prepare_ack(value):
    return EffectPreparedReceipt(
        effect_key=value.effect_key,
        payload_digest=value.payload_digest,
        request_digest=value.request_digest,
        revision=1,
        phase="prepared",
    )


class Counter:
    def __init__(self):
        self.calls = 0

    async def execute(self, arguments, *, effect_key):
        self.calls += 1
        return EffectOutcome(
            phase="completed",
            external_ref="counter-order-1",
            evidence_ref="evidence-1",
            result_digest=digest(arguments),
        )


@pytest.fixture
async def journal(tmp_path):
    store = SQLiteEffectJournal(
        tmp_path / "effects.db", store_incarnation="kernel-store-a", initialize=True
    )
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_exact_result_is_durable_and_same_effect_never_executes_twice(journal):
    value, adapter = request(journal), Counter()
    executor = EffectExecutor(journal, authorize=authorize, prepare_remote=prepare_ack)
    first = await executor.invoke(value, ARGS, adapter)
    assert first.phase == "completed"
    assert (await executor.invoke(value, ARGS, adapter)) == first
    assert adapter.calls == 1
    reports = await journal.pending_reports()
    assert [row.phase for row in reports] == ["prepared", "unknown", "completed"]
    for row in reports:
        await journal.acknowledge_report(value.effect_key, row.revision, row.evidence_digest)
    assert await journal.pending_reports() == []
    assert await journal.unresolved() == []
    page = await journal.for_execution(value.context_ref, value.native_run_id, limit=2)
    assert [row.revision for row in page] == [1, 2]
    assert await journal.for_execution(
        value.context_ref, value.native_run_id, after=value.effect_key + ":2"
    ) == [first]
    assert await journal.for_execution(value.context_ref, value.native_run_id) == reports
    assert await journal.for_execution(value.context_ref, "another-native") == []
    assert (
        await journal.for_execution(
            value.context_ref, value.native_run_id, after=value.effect_key + ":3"
        )
        == []
    )


async def test_invocation_identity_is_durable_and_not_forged_by_changing_effect_index(journal):
    value = request(journal)
    assert await journal.get_invocation(value) is False
    assert await journal.record_invocation(value) == value.invocation_digest
    assert await journal.get_invocation(value) is True
    second = value.model_copy(update={"effect_index": 1})
    assert second.effect_key != value.effect_key
    assert await journal.record_invocation(second) == value.invocation_digest
    with pytest.raises(EffectConflict, match="effect_invocation_conflict"):
        await journal.record_invocation(
            value.model_copy(update={"payload_digest": digest("other")})
        )


async def test_report_drain_validates_exact_ack_and_retains_lost_or_cancelled_ack(journal):
    req = request(journal)
    await journal.prepare(req)

    async def remote(record):
        return EffectReportReceipt(
            effectKey=record.request.effect_key,
            journalRevision=record.revision,
            evidenceDigest=record.evidence_digest,
            authorityRevision=2,
            phase=record.phase,
        )

    async def forged(record):
        return (await remote(record)).model_copy(update={"evidenceDigest": digest("foreign")})

    with pytest.raises(EffectConflict, match="effect_report_ack_mismatch"):
        await drain_effect_reports(journal, forged)
    assert len(await journal.pending_reports()) == 1

    async def lost(record):
        await remote(record)
        raise ConnectionResetError("untrusted secret-bearing transport message")

    with pytest.raises(EffectUncertain, match="effect_report_unconfirmed"):
        await drain_effect_reports(journal, lost)

    async def stalled(_):
        await asyncio.Event().wait()

    with pytest.raises(EffectUncertain, match="effect_report_unconfirmed"):
        await drain_effect_reports(journal, stalled, timeout_seconds=0.01)
    task = asyncio.create_task(drain_effect_reports(journal, stalled))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await journal.pending_reports()) == 1
    assert await drain_effect_reports(journal, remote) == 1
    assert await journal.pending_reports() == []


async def test_report_drain_is_bounded_and_acknowledges_only_selected_batch(journal):
    first, second = request(journal), request(journal, tool_call_id="call-b")
    for req in (first, second):
        await journal.prepare(req)

    async def remote(record):
        return EffectReportReceipt(
            effectKey=record.request.effect_key,
            journalRevision=record.revision,
            evidenceDigest=record.evidence_digest,
            authorityRevision=2,
            phase=record.phase,
        )

    assert await drain_effect_reports(journal, remote, limit=1) == 1
    assert len(await journal.pending_reports()) == 1
    with pytest.raises(ValueError):
        await drain_effect_reports(journal, remote, limit=101)


@pytest.mark.parametrize("host_harness", ["postgres"], indirect=True)
async def test_native_pg_two_independent_pods_share_once_only_claim_and_durable_reports(
    host_harness,
):
    import asyncpg

    from ksadk.kernel.postgres_store import PostgresAgentKernelStore

    h = host_harness
    first = await PostgresEffectJournal.open(
        h.store, store_incarnation=h.incarnation, initialize=True
    )
    pool = await asyncpg.create_pool(h.sessions.dsn)
    try:
        second = await PostgresEffectJournal.open(
            PostgresAgentKernelStore(pool, None, tenant_id=h.store.tenant_id),
            store_incarnation=h.incarnation,
        )
        assert second.journal_incarnation == first.journal_incarnation
        value, adapter = request(first), Counter()
        results = await asyncio.gather(
            *(
                EffectExecutor(store, authorize=authorize, prepare_remote=prepare_ack).invoke(
                    value, ARGS, adapter
                )
                for store in (first, second)
            ),
            return_exceptions=True,
        )
        assert adapter.calls == 1
        assert all(isinstance(item, (EffectRecord, EffectUncertain)) for item in results)
        assert (await second.get(value)).phase == "completed"
        assert [
            row.phase for row in await second.for_execution(value.context_ref, value.native_run_id)
        ] == ["prepared", "unknown", "completed"]
        assert await second.for_execution("other-context", value.native_run_id) == []
        assert (
            await second.for_execution(
                value.context_ref, value.native_run_id, after=value.effect_key + ":3"
            )
            == []
        )
        assert await second.get_invocation(value)
        reports = await second.pending_reports()
        assert [r.phase for r in reports] == ["prepared", "unknown", "completed"]
        for row in reports:
            await first.acknowledge_report(value.effect_key, row.revision, row.evidence_digest)
        assert await second.pending_reports() == []
        # ACK does not delete historical evidence required by recovery.
        page = await second.for_execution(value.context_ref, value.native_run_id, limit=2)
        assert [row.revision for row in page] == [1, 2]
        remaining = await second.for_execution(
            value.context_ref, value.native_run_id, after=value.effect_key + ":2"
        )
        assert [row.revision for row in remaining] == [3]
        with pytest.raises(EffectConflict, match="effect_report_ack_mismatch"):
            await second.acknowledge_report(value.effect_key, 3, digest("wrong"))
    finally:
        await pool.close()


@pytest.mark.parametrize("host_harness", ["postgres"], indirect=True)
async def test_native_pg_unknown_survives_restart_and_never_expires_to_replay(host_harness):
    h = host_harness
    first = await PostgresEffectJournal.open(
        h.store, store_incarnation=h.incarnation, initialize=True
    )
    value, adapter = request(first), Counter()
    await first.record_invocation(value)
    await first.prepare(value)
    await first.claim(value, "original-pod")
    reopened = await PostgresEffectJournal.open(h.store, store_incarnation=h.incarnation)
    with pytest.raises(EffectUncertain, match="effect_unknown_requires_reconciliation"):
        await EffectExecutor(reopened, authorize=authorize, prepare_remote=prepare_ack).invoke(
            value, ARGS, adapter
        )
    assert adapter.calls == 0
    assert await reopened.claim(value, "new-pod") is None
    assert (await reopened.unresolved())[0].phase == "unknown"
    with pytest.raises(EffectConflict, match="effect_claim_mismatch"):
        await reopened.settle(
            value, "new-pod", await adapter.execute(ARGS, effect_key=value.effect_key)
        )


@pytest.mark.parametrize("host_harness", ["postgres"], indirect=True)
async def test_native_pg_original_store_and_journal_replacement_are_fenced(host_harness):
    from ksadk.kernel.postgres_store import PostgresAgentKernelStore
    from ksadk.kernel.teams_execution_context import PostgresTeamsExecutionContextRegistry

    h = host_harness
    journal = await PostgresEffectJournal.open(
        h.store, store_incarnation=h.incarnation, initialize=True
    )
    value = request(journal)
    await journal.prepare(value)
    with pytest.raises(EffectConflict, match="effect_store_incarnation_mismatch"):
        await PostgresEffectJournal.open(h.store, store_incarnation="wrong", initialize=True)
    # Each tenant keeps separate original Kernel and effect-journal identity.
    other = PostgresAgentKernelStore(h.sessions._pool, None, tenant_id="tenant-two")
    other_incarnation = await PostgresTeamsExecutionContextRegistry(other).initialize()
    isolated = await PostgresEffectJournal.open(
        other, store_incarnation=other_incarnation, initialize=True
    )
    assert isolated.journal_incarnation != journal.journal_incarnation
    assert await isolated.unresolved() == []
    with pytest.raises(EffectConflict, match="effect_store_incarnation_mismatch"):
        await isolated.get(value)
    async with h.store._connection() as db:
        await db.execute(
            "UPDATE teams_effect_pg_meta SET journal_incarnation='recreated' WHERE namespace=$1",
            h.store.tenant_id,
        )
    with pytest.raises(EffectConflict, match="effect_store_incarnation_mismatch"):
        await journal.get(value)


@pytest.mark.parametrize("host_harness", ["postgres"], indirect=True)
async def test_native_pg_partial_persistence_rolls_back_and_corruption_fails_closed(
    host_harness, monkeypatch
):
    h = host_harness
    journal = await PostgresEffectJournal.open(
        h.store, store_incarnation=h.incarnation, initialize=True
    )
    value = request(journal)
    original = journal._persist

    async def broken(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("crash before PG commit")

    monkeypatch.setattr(journal, "_persist", broken)
    with pytest.raises(RuntimeError):
        await journal.prepare(value)
    assert await journal.get(value) is None
    assert await journal.pending_reports() == []
    monkeypatch.setattr(journal, "_persist", original)
    await journal.prepare(value)
    async with h.store._connection() as db:
        await db.execute(
            "UPDATE teams_effect_pg_entries SET phase='completed' WHERE namespace=$1",
            h.store.tenant_id,
        )
    with pytest.raises(EffectConflict, match="effect_evidence_corrupt"):
        await journal.claim(value, "never-send")


@pytest.mark.asyncio
async def test_no_external_write_before_server_prepared_ack(journal):
    adapter, value = Counter(), request(journal)

    async def missing_ack(_):
        raise ConnectionError("secret transport url must not be stored")

    executor = EffectExecutor(journal, authorize=authorize, prepare_remote=missing_ack)
    with pytest.raises(EffectUncertain, match="effect_prepare_unconfirmed"):
        await executor.invoke(value, ARGS, adapter)
    assert adapter.calls == 0
    assert (await journal.get(value)).phase == "prepared"
    assert "secret transport" not in journal.path.read_bytes().decode(errors="ignore")
    # A prepared record is never dispatched: an ACK-only retry is safe.
    retry = EffectExecutor(journal, authorize=authorize, prepare_remote=prepare_ack)
    assert (await retry.invoke(value, ARGS, adapter)).phase == "completed"
    assert adapter.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("effect_key", "wrong-effect"),
        ("payload_digest", "sha256:" + "a" * 64),
        ("request_digest", "sha256:" + "b" * 64),
        ("phase", "completed"),
    ],
)
async def test_foreign_or_settled_prepare_receipt_never_authorizes_another_write(
    journal, field, value
):
    adapter, req = Counter(), request(journal)

    async def wrong_ack(row):
        original = (await prepare_ack(row)).model_dump()
        return EffectPreparedReceipt(**{**original, field: value})

    with pytest.raises(EffectConflict):
        await EffectExecutor(journal, authorize=authorize, prepare_remote=wrong_ack).invoke(
            req, ARGS, adapter
        )
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_grant_revoked_while_preparing_prevents_external_write(journal):
    live, adapter, req = True, Counter(), request(journal)

    async def check(_):
        if not live:
            raise EffectConflict("grant_revoked")

    async def ack(row):
        nonlocal live
        live = False
        return await prepare_ack(row)

    with pytest.raises(EffectConflict, match="grant_revoked"):
        await EffectExecutor(journal, authorize=check, prepare_remote=ack).invoke(
            req, ARGS, adapter
        )
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_changed_payload_ref_or_adapter_cannot_bypass_original_effect(journal):
    original = request(journal)
    await journal.prepare(original)
    changed = {"target": "other", "increment": 1}
    candidates = [
        request(
            journal,
            payload_digest=effect_payload_digest("counter_increment", "counter/v1", changed),
        ),
        request(journal, adapter_version="counter/v2"),
        request(journal, context_ref="other-context"),
    ]
    for candidate in candidates:
        assert candidate.effect_key == original.effect_key
        with pytest.raises(EffectConflict, match="effect_payload_conflict"):
            await journal.prepare(candidate)
    renewed = request(
        journal,
        ref=TeamsExecutionRef.model_validate({**REF, "schedulerEpoch": REF["schedulerEpoch"] + 1}),
    )
    assert (await journal.prepare(renewed)).request.effect_key == original.effect_key
    with pytest.raises(EffectConflict):
        await EffectExecutor(journal, authorize=authorize, prepare_remote=prepare_ack).invoke(
            original, changed, Counter()
        )


@pytest.mark.asyncio
async def test_two_independent_connections_have_only_one_external_sender(journal):
    second = SQLiteEffectJournal(journal.path, store_incarnation=journal.store_incarnation)
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldCounter(Counter):
        async def execute(self, arguments, *, effect_key):
            entered.set()
            await release.wait()
            return await super().execute(arguments, effect_key=effect_key)

    adapter, req = HeldCounter(), request(journal)
    first = asyncio.create_task(
        EffectExecutor(journal, authorize=authorize, prepare_remote=prepare_ack).invoke(
            req, ARGS, adapter
        )
    )
    await entered.wait()
    with pytest.raises(EffectUncertain):
        await EffectExecutor(second, authorize=authorize, prepare_remote=prepare_ack).invoke(
            req, ARGS, adapter
        )
    release.set()
    assert (await first).phase == "completed"
    assert adapter.calls == 1
    await second.close()


@pytest.mark.asyncio
async def test_unknown_survives_cancellation_and_does_not_expire_into_replay(journal):
    entered = asyncio.Event()

    class HungAdapter:
        async def execute(self, arguments, *, effect_key):
            entered.set()
            await asyncio.Event().wait()

    req = request(journal)
    task = asyncio.create_task(
        EffectExecutor(journal, authorize=authorize, prepare_remote=prepare_ack).invoke(
            req, ARGS, HungAdapter()
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await journal.get(req)).phase == "unknown"
    second = SQLiteEffectJournal(journal.path, store_incarnation=journal.store_incarnation)
    adapter = Counter()
    with pytest.raises(EffectUncertain):
        await EffectExecutor(second, authorize=authorize, prepare_remote=prepare_ack).invoke(
            req, ARGS, adapter
        )
    assert adapter.calls == 0
    await second.close()


@pytest.mark.asyncio
async def test_failed_requires_definitive_no_application_proof(journal):
    with pytest.raises(ValueError, match="effect_outcome_not_proven"):
        EffectOutcome(phase="failed", evidence_ref="timeout")

    class Declined:
        async def execute(self, arguments, *, effect_key):
            return EffectOutcome(
                phase="failed", evidence_ref="declined-before-apply", definitively_not_applied=True
            )

    result = await EffectExecutor(journal, authorize=authorize, prepare_remote=prepare_ack).invoke(
        request(journal), ARGS, Declined()
    )
    assert result.phase == "failed"


@pytest.mark.asyncio
async def test_report_outbox_is_durable_scoped_and_exactly_acknowledged(journal):
    req = request(journal)
    await journal.prepare(req)
    await journal.claim(req, "owner-a")
    outcome = EffectOutcome(phase="completed", evidence_ref="proof-a")
    with pytest.raises(EffectConflict, match="effect_claim_mismatch"):
        await journal.settle(req, "owner-b", outcome)
    result = await journal.settle(req, "owner-a", outcome)
    assert await journal.settle(req, "owner-a", outcome) == result
    with pytest.raises(EffectConflict, match="effect_outcome_conflict"):
        await journal.settle(
            req, "owner-a", EffectOutcome(phase="completed", evidence_ref="changed")
        )
    with pytest.raises(EffectConflict, match="effect_report_ack_mismatch"):
        await journal.acknowledge_report(req.effect_key, result.revision, "sha256:" + "0" * 64)
    second = SQLiteEffectJournal(journal.path, store_incarnation=journal.store_incarnation)
    assert len(await second.pending_reports()) == 3
    for row in await second.pending_reports():
        await second.acknowledge_report(req.effect_key, row.revision, row.evidence_digest)
    assert await journal.pending_reports() == []
    await second.close()


@pytest.mark.asyncio
async def test_recreated_journal_does_not_match_old_server_receipt(journal, tmp_path):
    req, adapter = request(journal), Counter()
    receipt = await prepare_ack(req)
    fresh = SQLiteEffectJournal(
        tmp_path / "replacement.db", store_incarnation=journal.store_incarnation, initialize=True
    )
    replacement = request(fresh)
    assert req.effect_key == replacement.effect_key
    assert req.request_digest != replacement.request_digest

    async def old_ack(_):
        return receipt

    with pytest.raises(EffectConflict, match="effect_prepare_ack_mismatch"):
        await EffectExecutor(fresh, authorize=authorize, prepare_remote=old_ack).invoke(
            replacement, ARGS, adapter
        )
    assert adapter.calls == 0
    await fresh.close()
    with pytest.raises(EffectConflict, match="effect_store_incarnation_mismatch"):
        SQLiteEffectJournal(journal.path, store_incarnation="other-store")
    with pytest.raises(EffectConflict, match="effect_journal_unavailable"):
        SQLiteEffectJournal(tmp_path / "missing.db", store_incarnation="kernel-store-a")


@pytest.mark.asyncio
async def test_unresolved_page_is_bounded_and_journal_never_persists_arguments(journal):
    for index in range(3):
        await journal.prepare(request(journal, tool_call_id=f"call-{index}"))
    first = await journal.unresolved(limit=2)
    last = await journal.unresolved(limit=2, after=first[-1].request.effect_key)
    assert len(first) == 2 and len(last) == 1
    assert len({row.request.effect_key for row in first + last}) == 3
    with pytest.raises(ValueError):
        await journal.unresolved(limit=101)
    rows = journal.connection.execute("SELECT record_json FROM teams_effect_entries").fetchall()
    assert "disposable-counter" not in str([row[0] for row in rows])


@pytest.fixture
def external_counter():
    facts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            facts.append((self.headers["Idempotency-Key"], body))
            if self.path == "/drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"order":"counter-1"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", facts
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.asyncio
async def test_real_http_side_effect_accepted_then_response_lost_is_never_repeated(
    journal, external_counter
):
    base, facts = external_counter

    class External:
        async def execute(self, arguments, *, effect_key):
            def send():
                return urlopen(
                    Request(
                        base + "/drop",
                        data=json.dumps(arguments).encode(),
                        headers={"Idempotency-Key": effect_key},
                    ),
                    timeout=2,
                ).read()

            await asyncio.to_thread(send)
            raise AssertionError("server intentionally drops the response")

    req = request(journal)
    executor = EffectExecutor(journal, authorize=authorize, prepare_remote=prepare_ack)
    with pytest.raises(EffectUncertain, match="effect_execution_uncertain"):
        await executor.invoke(req, ARGS, External())
    assert len(facts) == 1 and facts[0][0] == req.effect_key
    with pytest.raises(EffectUncertain, match="effect_unknown_requires_reconciliation"):
        await executor.invoke(req, ARGS, External())
    assert len(facts) == 1
    assert (await journal.get(req)).phase == "unknown"


@pytest.mark.asyncio
async def test_process_crashes_after_actual_external_acceptance_and_reopen_blocks_replay(
    journal, external_counter, tmp_path
):
    base, facts = external_counter
    req = request(journal)
    request_file = tmp_path / "request.json"
    request_file.write_text(req.model_dump_json())
    child = r"""
import asyncio, json, os, sys
from urllib.request import Request, urlopen
from ksadk.kernel.teams_effects import *
async def main():
    req = EffectRequest.model_validate_json(open(sys.argv[2]).read())
    journal = SQLiteEffectJournal(sys.argv[1], store_incarnation=req.store_incarnation)
    async def authorize(_): pass
    async def ack(row):
        return EffectPreparedReceipt(
            effect_key=row.effect_key, payload_digest=row.payload_digest,
            request_digest=row.request_digest, revision=1, phase="prepared")
    class External:
        async def execute(self, args, *, effect_key):
            request = Request(sys.argv[3] + "/apply", data=json.dumps(args).encode(),
                headers={"Idempotency-Key":effect_key})
            with urlopen(request, timeout=3) as response:
                response.read()
            os._exit(73)
    executor = EffectExecutor(journal, authorize=authorize, prepare_remote=ack)
    await executor.invoke(req, {"target":"disposable-counter","increment":1}, External())
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", child, str(journal.path), str(request_file), base],
        capture_output=True,
        timeout=10,
        cwd=Path(__file__).parents[2],
    )
    assert result.returncode == 73, result.stderr.decode()
    assert len(facts) == 1
    reopened = SQLiteEffectJournal(journal.path, store_incarnation=journal.store_incarnation)
    adapter = Counter()
    assert (await reopened.get(req)).phase == "unknown"
    with pytest.raises(EffectUncertain):
        await EffectExecutor(reopened, authorize=authorize, prepare_remote=prepare_ack).invoke(
            req, ARGS, adapter
        )
    assert adapter.calls == 0 and len(facts) == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_storage_claim_wait_does_not_bypass_the_last_live_grant_check(journal):
    calls, adapter, req = 0, Counter(), request(journal)

    async def expires_before_io(_):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise EffectConflict("grant_expired_after_claim")

    with pytest.raises(EffectUncertain):
        await EffectExecutor(
            journal, authorize=expires_before_io, prepare_remote=prepare_ack
        ).invoke(req, ARGS, adapter)
    assert adapter.calls == 0
    assert (await journal.get(req)).phase == "unknown"


@pytest.mark.asyncio
async def test_revoked_caller_cannot_read_cached_effect_through_executor(journal):
    adapter, req = Counter(), request(journal)
    await EffectExecutor(journal, authorize=authorize, prepare_remote=prepare_ack).invoke(
        req, ARGS, adapter
    )

    async def denied(_):
        raise EffectConflict("caller_scope_revoked")

    with pytest.raises(EffectConflict, match="caller_scope_revoked"):
        await EffectExecutor(journal, authorize=denied, prepare_remote=prepare_ack).invoke(
            req, ARGS, adapter
        )
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_corrupt_row_identity_is_never_used_as_completion_proof(journal):
    req = request(journal)
    await journal.prepare(req)
    journal.connection.execute(
        "UPDATE teams_effect_entries SET phase='completed' WHERE effect_key=?", (req.effect_key,)
    )
    with pytest.raises(EffectConflict, match="effect_evidence_corrupt"):
        await journal.get(req)


@pytest.mark.asyncio
async def test_claimed_before_external_call_is_still_unknown_on_restart(journal):
    req, adapter = request(journal), Counter()
    await journal.prepare(req)
    await journal.claim(req, "crashed-before-send")
    restarted = SQLiteEffectJournal(journal.path, store_incarnation=journal.store_incarnation)
    with pytest.raises(EffectUncertain):
        await EffectExecutor(restarted, authorize=authorize, prepare_remote=prepare_ack).invoke(
            req, ARGS, adapter
        )
    assert adapter.calls == 0
    await restarted.close()
