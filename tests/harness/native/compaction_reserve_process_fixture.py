"""Stop after committing an unfinished summary; the parent really SIGKILLs us."""

import asyncio
import json
import os
import sys
from pathlib import Path

from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.state import RunPhase
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.test_compaction_finalization_reserve import (
    DeliveryModel,
    ObservationTools,
    configured,
    context_planner,
)
from tests.harness.native.test_kernel import SCOPE


class FileObservationTools(ObservationTools):
    def __init__(self, path):
        super().__init__()
        self.path = path

    async def execute(self, request):
        with self.path.open("ab") as stream:
            stream.write(b"effect\n")
            stream.flush()
            os.fsync(stream.fileno())
        return await super().execute(request)


async def main(root):
    store = SQLiteRunStore(root / "reserve.sqlite")
    model = DeliveryModel(summary_finish="length", observe_before_summary=True)
    tools = FileObservationTools(root / "effect.log")
    kernel = NativeKernel(
        store=store,
        scope=SCOPE,
        configuration=configured(calls=4),
        model=model,
        tools=tools,
        context_planner=context_planner(),
        lease_ttl_seconds=30,
    )
    run = kernel.start(goal="Collect evidence and deliver the result", session_id="s")
    for _ in range(40):
        snapshot = await kernel.advance(run.run_id)
        if (
            snapshot.phase == RunPhase.COMPACTING
            and snapshot.model_calls == 3
            and snapshot.pending_intent is None
        ):
            assert [r.purpose for r in model.requests] == ["task", "task", "compaction"]
            assert tools.content in str(model.requests[1].messages)
            assert snapshot.tool_calls == len(tools.keys) == 1
            assert snapshot.compaction_request is not None
            assert not snapshot.compaction_history.get("count", 0)
            marker = {
                "run_id": run.run_id,
                "fence": kernel.fencing_token(run.run_id),
                "snapshot": snapshot.model_dump(mode="json"),
                "request_purposes": [r.purpose for r in model.requests],
                "task_observed_complete_receipt": True,
            }
            (root / "ready.tmp").write_text(json.dumps(marker))
            (root / "ready.tmp").replace(root / "ready.json")
            # No release, close, lease shortening or in-process recovery.
            await asyncio.Event().wait()
    raise AssertionError("summary retry checkpoint was not reached")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
