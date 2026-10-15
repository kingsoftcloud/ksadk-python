"""SIGKILL at summary retry/coverage boundaries with an unchanged 30-second lease."""

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ksadk.harness.stores.native_contract import LeaseBusy
from ksadk.harness.stores.native_memory import MemoryRunStore
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.test_context_summary_recovery import SummaryRecoveryModel, recovery_kernel
from tests.harness.native.test_kernel import SCOPE
from tests.studio.native_recovery_lease_fixture import RecoveryLeaseAudit

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="requires SIGKILL"),
    pytest.mark.local_process_heavy,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["retry_committed", "retry_prepared", "chunk_committed"])
async def test_sigkill_preserves_repartition_coverage_and_original_retry_budget(
    tmp_path, monkeypatch, boundary,
):
    with (tmp_path / "worker.log").open("w") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.harness.native.summary_recovery_process_fixture",
             str(tmp_path), boundary], cwd=Path(__file__).resolve().parents[3],
            stdout=output, stderr=output,
        )
        try:
            async with asyncio.timeout(15):
                while not (tmp_path / "ready.json").exists():
                    assert child.poll() is None, (tmp_path / "worker.log").read_text()
                    await asyncio.sleep(0.02)
            marker = json.loads((tmp_path / "ready.json").read_text())
            child.kill()
            assert await asyncio.to_thread(child.wait, timeout=5) == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                await asyncio.to_thread(child.wait, timeout=5)

    with SQLiteRunStore(tmp_path / "summary.sqlite") as store:
        before = store.load(SCOPE, marker["handle"]["run_id"])
        prefix = store.events(SCOPE, before.run_id)
        receipts = {
            e.payload["operation_key"]: store.get_receipt(
                SCOPE, before.run_id, e.payload["operation_key"]
            )
            for e in prefix if e.kind == "effect.received"
        }
        audit = RecoveryLeaseAudit(store, marker, tmp_path)
        audit.observe_dispatch(monkeypatch)
        model = SummaryRecoveryModel(failed=True)
        kernel, run_id = recovery_kernel(store, model, lease=30, attach=False)
        with pytest.raises(LeaseBusy):
            kernel.attach(run_id)
        async with asyncio.timeout(35):
            while True:
                try:
                    kernel.attach(run_id)
                    break
                except LeaseBusy:
                    await asyncio.sleep(0.1)
        assert kernel.snapshot(run_id) == before
        result = await kernel.run_until_idle(run_id)
        audit.validate()
        assert result.compaction_history["count"] == 1 and result.model_calls == 4
        assert marker["model_requests"] + len(model.requests) == 4
        assert result.messages[:len(before.messages)] == before.messages
        assert kernel.events(run_id)[:len(prefix)] == prefix
        assert all(store.get_receipt(SCOPE, run_id, key) == value
                   for key, value in receipts.items())
        assert sum(e.kind == "context.compaction_retry_scheduled"
                   for e in kernel.events(run_id)) == 1
        assert sum(e.kind == "context.compaction_completed" for e in kernel.events(run_id)) == 1
        assert all(request.effective_max_output_tokens == 1024 for request in model.requests)
        kernel.release(run_id)

    baseline_model = SummaryRecoveryModel()
    with MemoryRunStore() as baseline_store:
        baseline, run_id = recovery_kernel(baseline_store, baseline_model, create=True)
        control = await baseline.run_until_idle(run_id)
        assert (result.final_text, result.outcome, result.model_calls) == (
            control.final_text, control.outcome, control.model_calls,
        )
        assert result.outcome != "verified"
        baseline.release(run_id)
