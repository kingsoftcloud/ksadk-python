"""Finalization reserve survives SIGKILL and expiry of a real 30-second lease."""

import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.state import RunPhase
from ksadk.harness.stores.native_contract import StoreConflict
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.compaction_reserve_process_fixture import FileObservationTools
from tests.harness.native.test_compaction_finalization_reserve import (
    DeliveryModel,
    ObservationTools,
    configured,
    context_planner,
    make_kernel,
)
from tests.harness.native.test_kernel import SCOPE


@pytest.mark.asyncio
@pytest.mark.local_process_heavy
async def test_sigkill_summary_retry_preserves_last_delivery_and_committed_effect(tmp_path):
    root = Path(__file__).resolve().parents[3]
    with (tmp_path / "worker.log").open("w") as log:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.harness.native.compaction_reserve_process_fixture",
                str(tmp_path),
            ],
            cwd=root,
            stdout=log,
            stderr=log,
        )
        try:
            async with asyncio.timeout(15):
                while not (tmp_path / "ready.json").exists():
                    assert worker.poll() is None, (tmp_path / "worker.log").read_text()
                    await asyncio.sleep(0.02)
            marker = json.loads((tmp_path / "ready.json").read_text())
            worker.kill()
            assert await asyncio.to_thread(worker.wait, timeout=5) == -signal.SIGKILL
        finally:
            if worker.poll() is None:
                worker.kill()
                await asyncio.to_thread(worker.wait, timeout=5)

    model = DeliveryModel(summary_finish="length", observe_before_summary=True)
    tools = FileObservationTools(tmp_path / "effect.log")
    with SQLiteRunStore(tmp_path / "reserve.sqlite") as store:
        run_id = marker["run_id"]
        before = store.load(SCOPE, run_id)
        assert before.model_dump(mode="json") == marker["snapshot"]
        assert marker["request_purposes"] == ["task", "task", "compaction"]
        assert marker["task_observed_complete_receipt"] is True
        assert before.model_calls == 3 and before.tool_calls == 1
        assert before.compaction_request is not None
        assert not before.compaction_history.get("count", 0)
        prefix = store.events(SCOPE, run_id)
        receipts = {
            event.payload["operation_key"]: store.get_receipt(
                SCOPE,
                run_id,
                event.payload["operation_key"],
            )
            for event in prefix
            if event.kind == "effect.received"
        }
        kernel = NativeKernel(
            store=store,
            scope=SCOPE,
            configuration=configured(calls=4),
            model=model,
            tools=tools,
            context_planner=context_planner(),
            lease_ttl_seconds=30,
        )
        with pytest.raises(StoreConflict, match="active owner"):
            kernel.attach(run_id)
        async with asyncio.timeout(35):
            while True:
                try:
                    kernel.attach(run_id)
                    break
                except StoreConflict:
                    await asyncio.sleep(0.1)
        assert kernel.fencing_token(run_id) > marker["fence"]
        assert kernel.snapshot(run_id) == before
        result = await kernel.run_until_idle(run_id)
        assert result.phase == RunPhase.COMPLETED and result.model_calls == 4
        assert [request.purpose for request in model.requests] == ["task"]
        assert tools.content in str(model.requests[0].messages)
        assert "UNSEALED-SUMMARY" not in str(model.requests[0].messages)
        assert not tools.keys and (tmp_path / "effect.log").read_bytes() == b"effect\n"
        assert result.tool_calls == before.tool_calls == 1
        assert (result.configuration_digest, result.profile_digest, result.context_binding) == (
            before.configuration_digest,
            before.profile_digest,
            before.context_binding,
        )
        assert store.events(SCOPE, run_id)[: len(prefix)] == prefix
        assert all(
            store.get_receipt(SCOPE, run_id, key) == value for key, value in receipts.items()
        )
        assert sum(event.kind == "run.completed" for event in store.events(SCOPE, run_id)) == 1
        kernel.release(run_id)

    baseline_model = DeliveryModel(summary_finish="length", observe_before_summary=True)
    baseline = make_kernel(baseline_model, ObservationTools(), configured(calls=4))
    control = baseline.start(goal=before.goal, session_id="s")
    control = await baseline.run_until_idle(control.run_id)
    assert [r.purpose for r in baseline_model.requests] == [
        "task",
        "task",
        "compaction",
        "task",
    ]
    assert (result.final_text, result.outcome, result.model_calls) == (
        control.final_text,
        control.outcome,
        control.model_calls,
    )
