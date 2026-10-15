"""Real process checkpoint for the bounded summary retry/coverage protocol."""

import asyncio
import json
import os
import sys
from pathlib import Path

from ksadk.harness.native.state import RunPhase
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.test_context_summary_recovery import SummaryRecoveryModel, recovery_kernel
from tests.harness.native.test_kernel import SCOPE
from tests.studio.native_recovery_lease_fixture import lease_marker


async def main(root, boundary):
    store = SQLiteRunStore(root / "summary.sqlite")
    model = SummaryRecoveryModel()
    kernel, run_id = recovery_kernel(store, model, create=True, lease=30)
    for _ in range(30):
        snapshot = await kernel.advance(run_id)
        matches = snapshot.phase == RunPhase.COMPACTING and (
            (boundary == "retry_committed" and snapshot.model_calls == 1
             and snapshot.pending_intent is None)
            or (boundary == "retry_prepared" and snapshot.model_calls == 1
                and snapshot.pending_intent is not None)
            or (boundary == "chunk_committed" and snapshot.model_calls == 2
                and snapshot.pending_intent is None)
        )
        if not matches:
            continue
        marker = {
            "scope": SCOPE.model_dump(mode="json"), "handle": {"run_id": run_id},
            "boundary": boundary, "model_requests": len(model.requests),
            **lease_marker(store, SCOPE, run_id, mode="production"),
        }
        with (root / "ready.tmp").open("w") as output:
            json.dump(marker, output)
            output.flush()
            os.fsync(output.fileno())
        (root / "ready.tmp").replace(root / "ready.json")
        await asyncio.Event().wait()
    raise AssertionError("summary fault checkpoint was not reached")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
