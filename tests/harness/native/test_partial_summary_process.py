"""Actual SIGKILL before/after partial-summary CAS, with an original 30s lease."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ksadk.harness.native.context_runtime import CompactionRequest, _refs
from ksadk.harness.native.state import CommitBatch, RunPhase
from ksadk.harness.stores.native_contract import LeaseBusy, StoreConflict
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.partial_summary_process_fixture import (
    EVENT,
    SUMMARY,
    BoundaryStore,
    RecordedModel,
    export,
    make_kernel,
    raw_messages,
    write,
)
from tests.harness.native.test_kernel import SCOPE
from tests.studio.native_recovery_lease_fixture import RecoveryLeaseAudit


def assert_final(final, kernel, model):
    raw = raw_messages()
    assert final.phase == RunPhase.COMPLETED and final.outcome == "unverified"
    assert final.model_calls == 3 and final.tool_calls == 0 and final.descendant_model_calls == 0
    assert final.messages[:len(raw)] == raw
    history = final.compaction_history
    assert history["count"] == 1 and history["summary_text"] == SUMMARY
    assert history["retained_message_refs"] == list((_refs(raw)[0], *_refs(raw)[2:]))
    assert sum(e.kind == EVENT for e in kernel.events(final.run_id)) == 1
    assert sum(e.kind == "run.completed" for e in kernel.events(final.run_id)) == 1
    assert sum(e.kind == "context.compaction_chunk_completed"
               for e in kernel.events(final.run_id)) == 1
    assert not any(e.kind == "context.compaction_completed" for e in kernel.events(final.run_id))
    task = model.requests[-1]
    assert task.purpose == "task" and task.messages[-4:] == raw[2:]
    prepared = final.prepared_context
    assert prepared["counted_input_tokens"] + 1024 <= 8000
    assert prepared["messages"][-4:] == list(raw[2:])
    return {"input_tokens": prepared["counted_input_tokens"], "output_tokens": 1024,
            "window_tokens": 8000, "model_calls": final.model_calls,
            "outcome": final.outcome, "history_count": history["count"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
async def test_short_commit_hook_is_exact_and_baseline_keeps_full_tail(tmp_path, boundary):
    observed = []
    with BoundaryStore(tmp_path, boundary, pause=lambda: observed.append(True)) as store:
        model = RecordedModel(tmp_path)
        kernel, run_id = make_kernel(store, model, create=True)
        final = await kernel.run_until_idle(run_id)
        assert observed == [True]
        facts = json.loads((tmp_path / "boundary-logical.json").read_text())
        assert facts["snapshot"]["compaction_history"].get("count", 0) == (
            0 if boundary == "before_commit" else 1
        )
        assert_final(final, kernel, model)
        kernel.release(run_id)


@pytest.mark.asyncio
@pytest.mark.local_process_heavy
@pytest.mark.skipif(os.name != "posix", reason="requires real SIGKILL")
@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
async def test_real_sigkill_at_partial_summary_commit(tmp_path, monkeypatch, boundary):
    # Independent baseline has the identical source, configuration, and 30s TTL.
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    with SQLiteRunStore(baseline_dir / "partial.sqlite") as baseline_store:
        baseline_model = RecordedModel(baseline_dir)
        baseline, run_id = make_kernel(baseline_store, baseline_model, create=True)
        control = await baseline.run_until_idle(run_id)
        baseline_counts = assert_final(control, baseline, baseline_model)
        export(baseline_store, run_id, baseline_dir / "final-logical.json")
        baseline.release(run_id)

    worker_dir = tmp_path / "worker"
    lifecycle = {"boundary": boundary, "started_at": time.time(), "owned_pid": None,
                 "kill_sent": False, "returncode": None, "reaped": False}
    with (tmp_path / "worker.log").open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.harness.native.partial_summary_process_fixture",
             str(worker_dir), boundary],
            cwd=Path(__file__).resolve().parents[3], stdout=log, stderr=log,
        )
        try:
            lifecycle["owned_pid"] = child.pid
            write(tmp_path / "owned-process.json", lifecycle)
            async with asyncio.timeout(15):
                while not (worker_dir / "ready.json").exists():
                    assert child.poll() is None, (tmp_path / "worker.log").read_text()
                    await asyncio.sleep(0.02)
            marker = json.loads((worker_dir / "ready.json").read_text())
            assert marker["pid"] == child.pid and marker["boundary"] == boundary
            child.kill()
            lifecycle["kill_sent"] = True
            lifecycle["returncode"] = await asyncio.to_thread(child.wait, timeout=5)
            lifecycle["reaped"] = True
            assert lifecycle["returncode"] == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                lifecycle["cleanup_kill_sent"] = True
                lifecycle["returncode"] = await asyncio.to_thread(child.wait, timeout=5)
                lifecycle["reaped"] = True
            lifecycle["finished_at"] = time.time()
            write(tmp_path / "owned-process.json", lifecycle)

    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    proposal = CommitBatch.model_validate_json((worker_dir / "proposal.json").read_text())
    before_logical = json.loads((worker_dir / "precommit-logical.json").read_text())
    durable = json.loads((worker_dir / "boundary-logical.json").read_text())
    with SQLiteRunStore(worker_dir / "partial.sqlite") as store:
        actual = export(store, run_id, resume_dir / "before-attach-logical.json")
        assert actual == durable  # No normalized coverage, source, event or receipt fields.
        before = store.load(SCOPE, run_id)
        request = CompactionRequest.model_validate(before_logical["snapshot"]["compaction_request"])
        assert request.summary_partition.covered_count == 1
        assert request.summary_partition.pending.stop > 1
        assert request.continuity.pending_tool_call_ids == ()
        assert before.messages == raw_messages()
        assert before.model_calls == 2 and before.pending_intent is None
        assert len([p for p in worker_dir.glob("model-*.json")]) == 2
        assert all(r and r["status"] == "committed" for r in actual["receipts"].values())
        audit = RecoveryLeaseAudit(store, marker, resume_dir)
        audit.observe_dispatch(monkeypatch)
        model = RecordedModel(resume_dir, resumed=True)
        kernel, _ = make_kernel(store, model, attach=False)
        with pytest.raises(LeaseBusy):
            kernel.attach(run_id)
        async with asyncio.timeout(35):
            while True:
                try:
                    kernel.attach(run_id)
                    break
                except LeaseBusy:
                    await asyncio.sleep(0.1)
        try:
            fence = kernel.fencing_token(run_id)
            assert fence > marker["fencing_token"]
            assert kernel.snapshot(run_id) == before and not model.requests
            with pytest.raises(StoreConflict, match="fenc"):
                store.commit(SCOPE, proposal, expected_revision=marker["expected_revision"],
                             fencing_token=marker["fencing_token"])
            assert export(store, run_id, resume_dir / "after-stale-fence-logical.json") == actual
            if boundary == "before_commit":
                assert before.compaction_history == {} and before.compaction_request is not None
                recomputed = kernel.context.begin_summary(before, kernel.configuration)
                assert recomputed == proposal  # Original source, coverage, config and budget.
                saved = await kernel.advance(run_id)
            else:
                assert before == proposal.snapshot and before.compaction_request is None
                saved = before
            assert saved == proposal.snapshot and saved.messages == before.messages
            assert not model.requests and saved.model_calls == before.model_calls == 2
            assert store.commit(SCOPE, proposal, expected_revision=marker["expected_revision"],
                                fencing_token=fence) == saved
            assert store.load(SCOPE, run_id) == saved
            assert sum(e.kind == EVENT for e in kernel.events(run_id)) == 1
            assert store.events(SCOPE, run_id)[:len(actual["events"])] == tuple(
                type(store.events(SCOPE, run_id)[0]).model_validate(e) for e in actual["events"]
            )
            assert all(store.get_receipt(SCOPE, run_id, key).model_dump(mode="json") == value
                       for key, value in actual["receipts"].items())
            final = await kernel.run_until_idle(run_id)
            audit.validate()
            counts = assert_final(final, kernel, model)
            assert [r.purpose for r in model.requests] == ["task"]
            assert counts == baseline_counts
            assert final.compaction_history == control.compaction_history
            assert (final.final_text, final.outcome, final.model_calls) == (
                control.final_text, control.outcome, control.model_calls,
            )
            end = export(store, run_id, resume_dir / "final-logical.json")
            assert all(end["receipts"][key] == value for key, value in actual["receipts"].items())
            effect_kinds = [end["intents"][key]["kind"] for key in end["receipts"]]
            assert effect_kinds.count("model") == 3
            assert effect_kinds.count("verify") == 1
            assert len(effect_kinds) == 4
            assert end == json.loads((baseline_dir / "final-logical.json").read_text())
            assert all(receipt["status"] == "committed" for receipt in end["receipts"].values())
            write(tmp_path / "comparison.json", {
                "boundary": boundary, "counts": counts, "baseline_equal": True,
                "before_after_raw_transcript_equal": True, "original_receipts_unchanged": True,
                "stale_fence_rejected": True, "idempotent_cas_equal": True,
                "partial_commit_event_count": 1, "worker_model_requests": 2,
                "resumed_model_requests": 1, "real_provider_calls": 0,
                "allowed_differences": ["lease owner/fence/expiry and audit timestamps",
                                        "process PID and output paths"],
            })
        finally:
            kernel.release(run_id)


@pytest.mark.asyncio
@pytest.mark.local_process_heavy
@pytest.mark.skipif(os.name != "posix", reason="requires actual child SIGKILL/reap")
async def test_first_owned_process_write_failure_preserves_error_and_reaps_child(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    module = sys.modules[__name__]
    original_popen, original_write = subprocess.Popen, write
    children = []
    failure = OSError("synthetic first owned-process write failure")
    owned_writes = 0
    observed = {"original_error_identity_preserved": False}

    def spawn(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail_first_owned_write(path, value):
        nonlocal owned_writes
        if path.name == "owned-process.json":
            owned_writes += 1
            if owned_writes == 1:
                assert len(children) == 1 and children[0].poll() is None
                raise failure
        return original_write(path, value)

    monkeypatch.setattr(module, "subprocess", SimpleNamespace(Popen=spawn))
    monkeypatch.setattr(module, "write", fail_first_owned_write)
    try:
        with pytest.raises(OSError) as caught:
            await test_real_sigkill_at_partial_summary_commit(
                tmp_path, monkeypatch, "before_commit",
            )
        observed["original_error_identity_preserved"] = caught.value is failure
        assert caught.value is failure
        assert len(children) == 1
        observed["returncode_before_test_fallback"] = children[0].returncode
        assert children[0].returncode == -signal.SIGKILL
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
        lifecycle = json.loads((tmp_path / "owned-process.json").read_text())
        assert lifecycle["cleanup_kill_sent"] and lifecycle["reaped"]
        assert lifecycle["returncode"] == -signal.SIGKILL
        assert owned_writes == 2
    finally:
        # The RED implementation leaks before entering its finally. The test
        # itself owns every spawned PID and must clean it even when assertions fail.
        for child in children:
            fallback = child.poll() is None
            if fallback:
                child.kill()
            await asyncio.to_thread(child.wait, timeout=5)
            observed.update(owned_pid=child.pid, test_fallback_kill_required=fallback,
                            final_returncode=child.returncode, final_reaped=True)
        original_write(tmp_path / "write-failure-cleanup.json", observed)
